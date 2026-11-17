"""Cache-aside em Redis. Falha do cache NUNCA derruba a consulta: cai para o banco."""

from __future__ import annotations

import logging
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from .observability import CACHE

logger = logging.getLogger(__name__)


class Cache(Protocol):
    async def obter(self, chave: str) -> bytes | None: ...

    async def gravar(self, chave: str, valor: bytes, ttl_s: int) -> None: ...

    async def fechar(self) -> None: ...


class RedisCache:
    def __init__(self, url: str):
        # Timeouts curtos: cache lento é pior que cache ausente.
        self._redis = Redis.from_url(url, socket_timeout=0.1, socket_connect_timeout=0.2)

    async def obter(self, chave: str) -> bytes | None:
        try:
            valor = await self._redis.get(chave)
        except (RedisError, OSError) as exc:
            CACHE.labels("erro").inc()
            logger.warning("cache indisponível na leitura: %s", exc)
            return None
        CACHE.labels("hit" if valor is not None else "miss").inc()
        return valor

    async def gravar(self, chave: str, valor: bytes, ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        try:
            await self._redis.set(chave, valor, ex=ttl_s)
        except (RedisError, OSError) as exc:
            CACHE.labels("erro").inc()
            logger.warning("cache indisponível na escrita: %s", exc)

    async def fechar(self) -> None:
        await self._redis.aclose()


class SemCache:
    async def obter(self, chave: str) -> bytes | None:
        return None

    async def gravar(self, chave: str, valor: bytes, ttl_s: int) -> None:
        return None

    async def fechar(self) -> None:
        return None
