"""Ponto de entrada: `uvicorn app.main:create_app --factory`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import api
from .config import Settings, get_settings
from .domain import (
    ConflitoIdempotencia,
    LancamentoJaEstornado,
    LancamentoNaoEncontrado,
    RegraDeNegocioViolada,
)
from .observability import ObservabilidadeMiddleware, configurar_logs
from .repository import LancamentoRepository
from .security import validar_configuracao
from .service import Relogio, ServicoLancamentos

logger = logging.getLogger(__name__)


@dataclass
class Dependencias:
    """Permite injetar dublês nos testes sem subir Postgres/RabbitMQ."""

    repo: LancamentoRepository
    relogio: Relogio | None = None
    pronto: Callable[[], Awaitable[bool]] | None = None


def relogio_padrao(fuso: str) -> Relogio:
    tz = ZoneInfo(fuso)
    return Relogio(
        agora=lambda: datetime.now(timezone.utc),
        hoje=lambda: datetime.now(tz).date(),
    )


def _problema(status_code: int, titulo: str, detalhe: str) -> JSONResponse:
    """Erros no formato Problem Details (RFC 9457), sem stack trace para o cliente."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "about:blank", "title": titulo, "status": status_code, "detail": detalhe},
        media_type="application/problem+json",
    )


def create_app(*, settings: Settings | None = None, dependencias: Dependencias | None = None) -> FastAPI:
    settings = settings or get_settings()
    configurar_logs(settings.log_level)
    # Fail fast: não sobe com autenticação mal configurada (sem chave pública ou com algoritmo simétrico).
    validar_configuracao(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        if dependencias is not None:
            app.state.servico = ServicoLancamentos(
                dependencias.repo, dependencias.relogio or relogio_padrao(settings.timezone)
            )
            app.state.pronto = dependencias.pronto or (lambda: _true())
            yield
            return

        from psycopg_pool import AsyncConnectionPool

        from .assinatura import carregar_chave_privada
        from .outbox import RabbitPublicador, executar_relay
        from .repository import PostgresLancamentoRepository, PostgresOutboxStore

        # Fail fast: valida a chave de assinatura antes de abrir conexões.
        chave_privada = None
        if settings.outbox_relay_enabled:
            if not settings.eventos_chave_privada:
                raise RuntimeError("LANC_EVENTOS_CHAVE_PRIVADA é obrigatória com o relay ligado")
            chave_privada = carregar_chave_privada(settings.eventos_chave_privada)

        pool = AsyncConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            open=False,
        )
        # O usuário de execução NÃO é dono das tabelas: as migrações rodam antes, em outro
        # container e com outro usuário (`python -m app.migrar`). Ver infra/postgres/init.
        await pool.open(wait=True, timeout=30)
        repo = PostgresLancamentoRepository(pool)
        app.state.servico = ServicoLancamentos(repo, relogio_padrao(settings.timezone))
        app.state.pronto = repo.saudavel

        parar = asyncio.Event()
        publicador = None
        tarefa_relay = None
        if chave_privada is not None:
            publicador = RabbitPublicador(settings.rabbitmq_url, settings.exchange, chave_privada)
            tarefa_relay = asyncio.create_task(
                executar_relay(
                    PostgresOutboxStore(pool),
                    publicador,
                    limite=settings.outbox_batch_size,
                    intervalo_s=settings.outbox_poll_interval_s,
                    parar=parar,
                )
            )
        logger.info("serviço de lançamentos iniciado")
        try:
            yield
        finally:
            parar.set()
            if tarefa_relay is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(tarefa_relay, timeout=10)
            if publicador is not None:
                await publicador.fechar()
            await pool.close()

    app = FastAPI(
        title="Fluxo de Caixa — Lançamentos",
        version="1.0.0",
        lifespan=lifespan,
        # Desligado por padrão (superfície de ataque). O prefixo evita colisão no gateway local.
        docs_url="/lancamentos/docs" if settings.docs_habilitado else None,
        openapi_url="/lancamentos/openapi.json" if settings.docs_habilitado else None,
        redoc_url=None,
    )
    app.add_middleware(ObservabilidadeMiddleware)
    app.include_router(api.router)

    @app.exception_handler(RegraDeNegocioViolada)
    async def _regra(_: Request, exc: RegraDeNegocioViolada):
        return _problema(422, "Regra de negócio violada", str(exc))

    @app.exception_handler(LancamentoNaoEncontrado)
    async def _nao_encontrado(_: Request, exc: LancamentoNaoEncontrado):
        return _problema(404, "Lançamento não encontrado", str(exc))

    @app.exception_handler(LancamentoJaEstornado)
    async def _ja_estornado(_: Request, exc: LancamentoJaEstornado):
        return _problema(409, "Lançamento já estornado", str(exc))

    @app.exception_handler(ConflitoIdempotencia)
    async def _conflito(_: Request, exc: ConflitoIdempotencia):
        return _problema(409, "Idempotency-Key reutilizada com outro conteúdo", str(exc))

    @app.get("/health/live", include_in_schema=False)
    async def vivo():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def pronto(request: Request):
        # Só depende do PRÓPRIO banco. Broker e Consolidado fora do ar NÃO tiram o serviço do ar.
        if await request.app.state.pronto():
            return {"status": "ok"}
        return JSONResponse({"status": "indisponivel"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.get("/metrics", include_in_schema=False)
    async def metricas():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


async def _true() -> bool:
    return True


__all__ = ["create_app", "Dependencias"]
