from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

import psycopg
from psycopg_pool import AsyncConnectionPool

from .domain import EventoLancamento, ReadModelIndisponivel, RelatorioDiario

# Banco fora, em failover, sem conexão no pool (PoolTimeout é OperationalError) ou transação
# abortada por deadlock/serialização: tentar de novo resolve; a mensagem não tem defeito.
_ERROS_DE_INFRA = (psycopg.OperationalError, psycopg.InterfaceError, OSError)


class ConsolidadoRepository(Protocol):
    async def aplicar_evento(self, evento: EventoLancamento) -> bool:
        """Aplica o evento ao saldo do dia. Retorna False se o event_id já foi processado."""

    async def relatorio(self, comerciante_id: str, data: date) -> RelatorioDiario: ...

    async def saudavel(self) -> bool: ...


class PostgresConsolidadoRepository:
    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def aplicar_evento(self, evento: EventoLancamento) -> bool:
        try:
            return await self._aplicar(evento)
        except _ERROS_DE_INFRA as exc:
            raise ReadModelIndisponivel(str(exc)) from exc

    async def _aplicar(self, evento: EventoLancamento) -> bool:
        # Dedupe + incremento atômico na MESMA transação: sem dupla contagem e sem lost update
        # mesmo com várias réplicas consumindo em paralelo.
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    INSERT INTO eventos_processados (event_id, lancamento_id, comerciante_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (evento.event_id, evento.lancamento_id, evento.comerciante_id),
                )
                if await cur.fetchone() is None:
                    return False
                await conn.execute(
                    """
                    INSERT INTO saldo_diario
                        (comerciante_id, data, total_creditos, total_debitos, quantidade, atualizado_em)
                    VALUES (%s, %s, %s, %s, 1, now())
                    ON CONFLICT (comerciante_id, data) DO UPDATE SET
                        total_creditos = saldo_diario.total_creditos + EXCLUDED.total_creditos,
                        total_debitos  = saldo_diario.total_debitos  + EXCLUDED.total_debitos,
                        quantidade     = saldo_diario.quantidade + 1,
                        atualizado_em  = now()
                    """,
                    (evento.comerciante_id, evento.data_competencia, evento.credito, evento.debito),
                )
        return True

    async def relatorio(self, comerciante_id: str, data: date) -> RelatorioDiario:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT
                    COALESCE(SUM(total_creditos) FILTER (WHERE data = %(d)s), 0),
                    COALESCE(SUM(total_debitos)  FILTER (WHERE data = %(d)s), 0),
                    COALESCE(SUM(quantidade)     FILTER (WHERE data = %(d)s), 0),
                    MAX(atualizado_em)           FILTER (WHERE data = %(d)s),
                    COALESCE(SUM(total_creditos - total_debitos), 0)
                FROM saldo_diario
                WHERE comerciante_id = %(c)s AND data <= %(d)s
                """,
                {"c": comerciante_id, "d": data},
            )
            cred, deb, qtd, atualizado, acumulado = await cur.fetchone()
        dois = Decimal("0.01")
        return RelatorioDiario.calcular(
            data=data,
            creditos=Decimal(cred).quantize(dois),
            debitos=Decimal(deb).quantize(dois),
            quantidade=int(qtd),
            saldo_acumulado=Decimal(acumulado).quantize(dois),
            atualizado_em=atualizado,
        )

    async def saudavel(self) -> bool:
        try:
            async with self._pool.connection(timeout=2) as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False
