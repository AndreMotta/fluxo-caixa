from __future__ import annotations

from datetime import date, datetime, timezone

from .cache import Cache
from .domain import EventoLancamento, RelatorioDiario
from .observability import DEFASAGEM, EVENTOS
from .repository import ConsolidadoRepository


class ConsultaConsolidado:
    """Lado de leitura (Query). Atende o pico de 50 req/s com read model pré-calculado + cache."""

    def __init__(self, repo: ConsolidadoRepository, cache: Cache, ttl_s: int):
        self._repo = repo
        self._cache = cache
        self._ttl_s = ttl_s

    @staticmethod
    def _chave(comerciante_id: str, data: date) -> str:
        return f"consolidado:v1:{comerciante_id}:{data.isoformat()}"

    async def relatorio(self, comerciante_id: str, data: date) -> RelatorioDiario:
        chave = self._chave(comerciante_id, data)
        em_cache = await self._cache.obter(chave)
        if em_cache is not None:
            try:
                return RelatorioDiario.de_json(em_cache)
            except (ValueError, KeyError):
                pass  # entrada corrompida: ignora e recalcula
        relatorio = await self._repo.relatorio(comerciante_id, data)
        await self._cache.gravar(chave, relatorio.to_json(), self._ttl_s)
        return relatorio


class ProjecaoSaldo:
    """Lado de escrita do read model: aplica eventos de Lançamentos ao saldo diário."""

    def __init__(self, repo: ConsolidadoRepository):
        self._repo = repo

    async def aplicar(self, evento: EventoLancamento) -> bool:
        aplicado = await self._repo.aplicar_evento(evento)
        EVENTOS.labels("aplicado" if aplicado else "duplicado").inc()
        if aplicado:
            atraso = (datetime.now(timezone.utc) - evento.occurred_at).total_seconds()
            DEFASAGEM.observe(max(atraso, 0))
        return aplicado
