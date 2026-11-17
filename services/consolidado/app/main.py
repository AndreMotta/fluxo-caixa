"""Ponto de entrada: `uvicorn app.main:create_app --factory`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import api
from .cache import Cache, SemCache
from .config import Settings, get_settings
from .observability import ObservabilidadeMiddleware, configurar_logs
from .repository import ConsolidadoRepository
from .security import validar_configuracao
from .service import ConsultaConsolidado

logger = logging.getLogger(__name__)


@dataclass
class Dependencias:
    repo: ConsolidadoRepository
    cache: Cache | None = None
    pronto: Callable[[], Awaitable[bool]] | None = None


async def _true() -> bool:
    return True


def create_app(*, settings: Settings | None = None, dependencias: Dependencias | None = None) -> FastAPI:
    settings = settings or get_settings()
    configurar_logs(settings.log_level)
    # Fail fast: não sobe com autenticação mal configurada (sem chave pública ou com algoritmo simétrico).
    validar_configuracao(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        if dependencias is not None:
            app.state.consulta = ConsultaConsolidado(
                dependencias.repo, dependencias.cache or SemCache(), settings.cache_ttl_s
            )
            app.state.pronto = dependencias.pronto or _true
            yield
            return

        from psycopg_pool import AsyncConnectionPool

        from .cache import RedisCache
        from .consumer import ConsumidorRabbit, carregar_chaves_publicas
        from .repository import PostgresConsolidadoRepository
        from .service import ProjecaoSaldo

        pool = AsyncConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            open=False,
        )
        # Usuário de execução sem DDL; migrações rodam antes, em job separado (`python -m app.migrar`).
        await pool.open(wait=True, timeout=30)
        repo = PostgresConsolidadoRepository(pool)
        cache = RedisCache(settings.redis_url)
        app.state.consulta = ConsultaConsolidado(repo, cache, settings.cache_ttl_s)
        app.state.pronto = repo.saudavel

        parar = asyncio.Event()
        consumidor = None
        tarefa = None
        if settings.consumidor_enabled:
            chaves = carregar_chaves_publicas(settings.eventos_chaves_publicas or "")
            if not chaves:
                raise RuntimeError("CONS_EVENTOS_CHAVES_PUBLICAS é obrigatória com o consumidor ligado")
            consumidor = ConsumidorRabbit(
                settings.rabbitmq_url,
                settings.fila,
                settings.prefetch,
                ProjecaoSaldo(repo),
                chaves,
                retencao_s=settings.retencao_indisponibilidade_s,
            )
            tarefa = asyncio.create_task(consumidor.executar(parar))
        logger.info("serviço de consolidado iniciado")
        try:
            yield
        finally:
            parar.set()
            if tarefa is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(tarefa, timeout=10)
            if consumidor is not None:
                await consumidor.fechar()
            await cache.fechar()
            await pool.close()

    app = FastAPI(
        title="Fluxo de Caixa — Consolidado Diário",
        version="1.0.0",
        lifespan=lifespan,
        # Desligado por padrão (superfície de ataque); só o ambiente local liga.
        docs_url="/consolidado/docs" if settings.docs_habilitado else None,
        openapi_url="/consolidado/openapi.json" if settings.docs_habilitado else None,
        redoc_url=None,
    )
    app.add_middleware(ObservabilidadeMiddleware)
    app.include_router(api.router)

    @app.get("/health/live", include_in_schema=False)
    async def vivo():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def pronto(request: Request):
        # Leitura depende só do próprio banco. Broker fora: consultas continuam (dados podem defasar).
        if await request.app.state.pronto():
            return {"status": "ok"}
        return JSONResponse({"status": "indisponivel"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.get("/metrics", include_in_schema=False)
    async def metricas():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
