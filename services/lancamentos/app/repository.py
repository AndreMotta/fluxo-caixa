"""Persistência do contexto de Lançamentos (PostgreSQL).

O lançamento e o evento de integração são gravados NA MESMA transação (Transactional Outbox).
Assim o registro nunca depende do broker nem do Consolidado estar no ar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .domain import (
    ConflitoIdempotencia,
    EventoLancamentoRegistrado,
    Lancamento,
    LancamentoJaEstornado,
    TipoLancamento,
)


@dataclass(frozen=True)
class ResultadoRegistro:
    lancamento: Lancamento
    criado: bool  # False = repetição idempotente (mesma Idempotency-Key e mesmo conteúdo)


class LancamentoRepository(Protocol):
    async def registrar(
        self,
        lancamento: Lancamento,
        evento: EventoLancamentoRegistrado,
        idempotency_key: str,
        hash_requisicao: str,
    ) -> ResultadoRegistro: ...

    async def obter(self, comerciante_id: str, lancamento_id: UUID) -> Lancamento | None: ...

    async def listar_por_data(
        self, comerciante_id: str, data: date, limite: int, deslocamento: int
    ) -> list[Lancamento]: ...

    async def saudavel(self) -> bool: ...


@dataclass(frozen=True)
class EventoOutbox:
    id: int
    event_id: str
    routing_key: str
    payload: bytes


class LoteOutbox(Protocol):
    eventos: list[EventoOutbox]

    async def marcar_publicados(self, ids: list[int]) -> None: ...


class OutboxStore(Protocol):
    def lote_pendente(self, limite: int) -> "AsyncIterator[LoteOutbox]": ...

    async def contar_pendentes(self) -> int: ...


def _linha_para_lancamento(row: dict) -> Lancamento:
    return Lancamento(
        id=row["id"],
        comerciante_id=row["comerciante_id"],
        tipo=TipoLancamento(row["tipo"]),
        valor=row["valor"],
        data_competencia=row["data_competencia"],
        descricao=row["descricao"],
        criado_em=row["criado_em"],
        criado_por=row["criado_por"],
        estorno_de=row["estorno_de"],
    )


_COLUNAS = "id, comerciante_id, tipo, valor, data_competencia, descricao, criado_em, criado_por, estorno_de"


class PostgresLancamentoRepository:
    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def registrar(
        self,
        lancamento: Lancamento,
        evento: EventoLancamentoRegistrado,
        idempotency_key: str,
        hash_requisicao: str,
    ) -> ResultadoRegistro:
        async with self._pool.connection() as conn:
            try:
                async with conn.transaction():
                    cur = await conn.execute(
                        f"""
                        INSERT INTO lancamentos ({_COLUNAS}, idempotency_key, hash_requisicao)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (comerciante_id, idempotency_key) DO NOTHING
                        RETURNING id
                        """,
                        (
                            lancamento.id,
                            lancamento.comerciante_id,
                            lancamento.tipo.value,
                            lancamento.valor,
                            lancamento.data_competencia,
                            lancamento.descricao,
                            lancamento.criado_em,
                            lancamento.criado_por,
                            lancamento.estorno_de,
                            idempotency_key,
                            hash_requisicao,
                        ),
                    )
                    if await cur.fetchone() is None:
                        return await self._repeticao(conn, lancamento.comerciante_id, idempotency_key, hash_requisicao)

                    await conn.execute(
                        """
                        INSERT INTO outbox (event_id, aggregate_id, routing_key, payload)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            evento.event_id,
                            lancamento.id,
                            evento.ROUTING_KEY,
                            Jsonb(evento.payload()),
                        ),
                    )
            except psycopg.errors.UniqueViolation as exc:
                if exc.diag.constraint_name == "uq_lancamentos_estorno_de":
                    raise LancamentoJaEstornado(str(lancamento.estorno_de)) from exc
                raise
        return ResultadoRegistro(lancamento=lancamento, criado=True)

    async def _repeticao(self, conn, comerciante_id: str, chave: str, hash_req: str) -> ResultadoRegistro:
        # row_factory por cursor (nunca na conexão): a conexão volta ao pool sem estado alterado.
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUNAS}, hash_requisicao FROM lancamentos "
                "WHERE comerciante_id = %s AND idempotency_key = %s",
                (comerciante_id, chave),
            )
            row = await cur.fetchone()
        if row["hash_requisicao"] != hash_req:
            raise ConflitoIdempotencia(chave)
        return ResultadoRegistro(lancamento=_linha_para_lancamento(row), criado=False)

    async def obter(self, comerciante_id: str, lancamento_id: UUID) -> Lancamento | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUNAS} FROM lancamentos WHERE comerciante_id = %s AND id = %s",
                (comerciante_id, lancamento_id),
            )
            row = await cur.fetchone()
        return _linha_para_lancamento(row) if row else None

    async def listar_por_data(
        self, comerciante_id: str, data: date, limite: int, deslocamento: int
    ) -> list[Lancamento]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUNAS} FROM lancamentos
                WHERE comerciante_id = %s AND data_competencia = %s
                ORDER BY criado_em, id
                LIMIT %s OFFSET %s
                """,
                (comerciante_id, data, limite, deslocamento),
            )
            rows = await cur.fetchall()
        return [_linha_para_lancamento(r) for r in rows]

    async def saudavel(self) -> bool:
        try:
            async with self._pool.connection(timeout=2) as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False


class _LotePostgres:
    def __init__(self, conn, eventos: list[EventoOutbox]):
        self._conn = conn
        self.eventos = eventos

    async def marcar_publicados(self, ids: list[int]) -> None:
        if ids:
            await self._conn.execute(
                "UPDATE outbox SET publicado_em = now() WHERE id = ANY(%s)", (ids,)
            )


class PostgresOutboxStore:
    """Lê eventos pendentes com FOR UPDATE SKIP LOCKED: várias réplicas do relay não se atropelam."""

    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    @asynccontextmanager
    async def lote_pendente(self, limite: int) -> AsyncIterator[_LotePostgres]:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT id, event_id, routing_key, payload::text
                    FROM outbox
                    WHERE publicado_em IS NULL
                    ORDER BY id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (limite,),
                )
                eventos = [
                    EventoOutbox(id=r[0], event_id=str(r[1]), routing_key=r[2], payload=r[3].encode())
                    for r in await cur.fetchall()
                ]
                yield _LotePostgres(conn, eventos)

    async def contar_pendentes(self) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM outbox WHERE publicado_em IS NULL")
            row = await cur.fetchone()
        return int(row[0])
