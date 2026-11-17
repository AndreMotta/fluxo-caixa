"""Aplica as migrações SQL com o usuário de MIGRAÇÃO (dono do schema).

Roda como job separado (`python -m app.migrar`), antes do serviço, que usa outro usuário sem DDL.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import psycopg

from .config import get_settings
from .observability import configurar_logs

MIGRACOES = Path(__file__).resolve().parent.parent / "migrations"
TRAVA = 727002

logger = logging.getLogger("migracao")


async def migrar(url: str) -> None:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (TRAVA,))
        try:
            for arquivo in sorted(MIGRACOES.glob("*.sql")):
                async with conn.transaction():
                    await conn.execute(arquivo.read_text(encoding="utf-8"))
                logger.info("migração aplicada: %s", arquivo.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (TRAVA,))


if __name__ == "__main__":
    s = get_settings()
    configurar_logs(s.log_level)
    asyncio.run(migrar(s.database_url))
