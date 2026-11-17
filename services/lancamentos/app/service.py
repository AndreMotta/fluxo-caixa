"""Casos de uso do contexto de Lançamentos."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from .domain import (
    EventoLancamentoRegistrado,
    Lancamento,
    LancamentoJaEstornado,
    LancamentoNaoEncontrado,
    TipoLancamento,
    hash_requisicao,
)
from .observability import LANCAMENTOS_REGISTRADOS
from .repository import LancamentoRepository, ResultadoRegistro

# Logger dedicado: em produção é roteado para armazenamento imutável (retenção longa, acesso restrito).
_auditoria = logging.getLogger("auditoria")


@dataclass
class Relogio:
    agora: Callable[[], datetime]
    hoje: Callable[[], date]


def _auditar(acao: str, resultado: ResultadoRegistro, idempotency_key: str) -> None:
    lanc = resultado.lancamento
    _auditoria.info(
        acao,
        extra={
            "campos": {
                "evento": "auditoria",
                "acao": acao,
                "lancamento_id": str(lanc.id),
                "comerciante_id": lanc.comerciante_id,
                "sujeito": lanc.criado_por,
                "tipo": lanc.tipo.value,
                "valor": str(lanc.valor),
                "data_competencia": lanc.data_competencia.isoformat(),
                "estorno_de": str(lanc.estorno_de) if lanc.estorno_de else None,
                "idempotency_key": idempotency_key,
                "repeticao_idempotente": not resultado.criado,
            }
        },
    )


class ServicoLancamentos:
    def __init__(self, repo: LancamentoRepository, relogio: Relogio):
        self._repo = repo
        self._relogio = relogio

    async def registrar(
        self,
        *,
        comerciante_id: str,
        tipo: TipoLancamento,
        valor: Decimal,
        data_competencia: date | None,
        descricao: str,
        idempotency_key: str,
        criado_por: str,
    ) -> ResultadoRegistro:
        hoje = self._relogio.hoje()
        lanc = Lancamento.novo(
            comerciante_id=comerciante_id,
            tipo=tipo,
            valor=valor,
            data_competencia=data_competencia or hoje,
            descricao=descricao,
            hoje=hoje,
            agora=self._relogio.agora(),
            criado_por=criado_por,
        )
        # Hash sobre os valores JÁ normalizados pelo domínio: "10" e "10.00" são o mesmo pedido.
        # A data entra como o CLIENTE enviou (None quando omitida), nunca o "hoje" do servidor:
        # um reenvio depois da meia-noite continua sendo o mesmo pedido e devolve o lançamento
        # original, em vez de 409 (que levaria o cliente a trocar a chave e duplicar o lançamento).
        dados = {
            "op": "registrar",
            "tipo": lanc.tipo.value,
            "valor": str(lanc.valor),
            "data_competencia": data_competencia.isoformat() if data_competencia else None,
            "descricao": lanc.descricao,
        }
        resultado = await self._repo.registrar(
            lanc, EventoLancamentoRegistrado(lanc), idempotency_key, hash_requisicao(dados)
        )
        if resultado.criado:
            LANCAMENTOS_REGISTRADOS.labels(tipo.value).inc()
        _auditar("lancamento_registrado", resultado, idempotency_key)
        return resultado

    async def estornar(
        self,
        *,
        comerciante_id: str,
        lancamento_id: UUID,
        motivo: str,
        idempotency_key: str,
        criado_por: str,
    ) -> ResultadoRegistro:
        original = await self._repo.obter(comerciante_id, lancamento_id)
        if original is None:
            raise LancamentoNaoEncontrado(str(lancamento_id))
        estorno = original.estornar(
            hoje=self._relogio.hoje(), agora=self._relogio.agora(), criado_por=criado_por, motivo=motivo
        )
        dados = {"op": "estornar", "lancamento_id": str(lancamento_id), "motivo": motivo}
        resultado = await self._repo.registrar(
            estorno, EventoLancamentoRegistrado(estorno), idempotency_key, hash_requisicao(dados)
        )
        if resultado.criado:
            LANCAMENTOS_REGISTRADOS.labels(estorno.tipo.value).inc()
        _auditar("lancamento_estornado", resultado, idempotency_key)
        return resultado

    async def obter(self, comerciante_id: str, lancamento_id: UUID) -> Lancamento:
        lanc = await self._repo.obter(comerciante_id, lancamento_id)
        if lanc is None:
            raise LancamentoNaoEncontrado(str(lancamento_id))
        return lanc

    async def listar(
        self, comerciante_id: str, data: date, limite: int, deslocamento: int
    ) -> list[Lancamento]:
        return await self._repo.listar_por_data(comerciante_id, data, limite, deslocamento)


__all__ = ["ServicoLancamentos", "Relogio", "LancamentoJaEstornado"]
