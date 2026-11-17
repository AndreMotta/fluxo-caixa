"""Modelo de domínio do contexto de Lançamentos.

Regras principais:
- Lançamento é imutável (append-only). Correção acontece por estorno, nunca por UPDATE/DELETE.
- Valor sempre positivo, com no máximo 2 casas decimais; o sinal vem do tipo (CRÉDITO/DÉBITO).
- Data de competência não pode estar no futuro.
- Estorno gera um lançamento de tipo inverso na data de hoje e só pode acontecer uma vez.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

VALOR_MAXIMO = Decimal("999999999999.99")
DUAS_CASAS = Decimal("0.01")
DESCRICAO_MAX = 140


class TipoLancamento(StrEnum):
    CREDITO = "CREDITO"
    DEBITO = "DEBITO"

    def inverso(self) -> "TipoLancamento":
        return TipoLancamento.DEBITO if self is TipoLancamento.CREDITO else TipoLancamento.CREDITO


class RegraDeNegocioViolada(Exception):
    """Entrada válida no formato, mas que fere uma regra de negócio (HTTP 422)."""


class LancamentoNaoEncontrado(Exception):
    """Lançamento inexistente para o comerciante (HTTP 404)."""


class LancamentoJaEstornado(Exception):
    """Tentativa de estornar duas vezes o mesmo lançamento (HTTP 409)."""


class ConflitoIdempotencia(Exception):
    """Idempotency-Key reutilizada com conteúdo diferente (HTTP 409)."""


@dataclass(frozen=True)
class Lancamento:
    id: UUID
    comerciante_id: str
    tipo: TipoLancamento
    valor: Decimal
    data_competencia: date
    descricao: str
    criado_em: datetime
    criado_por: str  # `sub` do token: trilha de auditoria (não-repúdio)
    estorno_de: UUID | None = None

    @staticmethod
    def novo(
        *,
        comerciante_id: str,
        tipo: TipoLancamento,
        valor: Decimal,
        data_competencia: date,
        descricao: str,
        hoje: date,
        agora: datetime,
        criado_por: str,
    ) -> "Lancamento":
        if not criado_por:
            raise RegraDeNegocioViolada("autor do lançamento é obrigatório")
        if valor <= 0:
            raise RegraDeNegocioViolada("valor deve ser maior que zero")
        if valor > VALOR_MAXIMO:
            raise RegraDeNegocioViolada("valor acima do limite permitido")
        if valor != valor.quantize(DUAS_CASAS):
            raise RegraDeNegocioViolada("valor deve ter no máximo 2 casas decimais")
        if data_competencia > hoje:
            raise RegraDeNegocioViolada("data de competência não pode estar no futuro")
        descricao = (descricao or "").strip()
        if len(descricao) > DESCRICAO_MAX:
            raise RegraDeNegocioViolada(f"descrição com mais de {DESCRICAO_MAX} caracteres")
        return Lancamento(
            id=uuid4(),
            comerciante_id=comerciante_id,
            tipo=tipo,
            valor=valor.quantize(DUAS_CASAS),
            data_competencia=data_competencia,
            descricao=descricao,
            criado_em=agora,
            criado_por=criado_por,
        )

    def estornar(self, *, hoje: date, agora: datetime, criado_por: str, motivo: str = "") -> "Lancamento":
        if self.estorno_de is not None:
            raise RegraDeNegocioViolada("não é possível estornar um estorno")
        descricao = f"Estorno de {self.id}" + (f": {motivo.strip()}" if motivo.strip() else "")
        return Lancamento(
            id=uuid4(),
            comerciante_id=self.comerciante_id,
            tipo=self.tipo.inverso(),
            valor=self.valor,
            data_competencia=hoje,
            descricao=descricao[:DESCRICAO_MAX],
            criado_em=agora,
            criado_por=criado_por,  # quem estornou, não quem lançou o original
            estorno_de=self.id,
        )


@dataclass(frozen=True)
class EventoLancamentoRegistrado:
    """Evento de integração publicado (via outbox) para os consumidores, como o Consolidado.

    É a "linguagem publicada" entre os contextos: o contrato é versionado e não expõe
    o modelo interno de persistência.
    """

    lancamento: Lancamento
    event_id: UUID = field(default_factory=uuid4)

    EVENT_TYPE = "LancamentoRegistrado"
    EVENT_VERSION = 1
    ROUTING_KEY = "lancamento.registrado.v1"

    def payload(self) -> dict:
        lanc = self.lancamento
        return {
            "event_id": str(self.event_id),
            "event_type": self.EVENT_TYPE,
            "event_version": self.EVENT_VERSION,
            "occurred_at": lanc.criado_em.isoformat(),
            "comerciante_id": lanc.comerciante_id,
            "lancamento_id": str(lanc.id),
            "tipo": lanc.tipo.value,
            "valor": str(lanc.valor),
            "data_competencia": lanc.data_competencia.isoformat(),
            "estorno_de": str(lanc.estorno_de) if lanc.estorno_de else None,
        }

    def to_json(self) -> bytes:
        return json.dumps(self.payload(), separators=(",", ":")).encode()


def hash_requisicao(dados: dict) -> str:
    """Hash canônico do corpo da requisição, usado para detectar reuso indevido de Idempotency-Key."""
    canonico = json.dumps(dados, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonico.encode()).hexdigest()
