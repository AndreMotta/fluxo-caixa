from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .domain import Lancamento, TipoLancamento


class LancamentoEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: TipoLancamento
    valor: Decimal = Field(gt=0, max_digits=14, decimal_places=2, examples=["150.75"])
    data_competencia: date | None = Field(
        default=None, description="Padrão: hoje no fuso do comerciante"
    )
    descricao: str = Field(default="", max_length=140)


class EstornoEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motivo: str = Field(default="", max_length=100)


class LancamentoSaida(BaseModel):
    id: UUID
    tipo: TipoLancamento
    valor: Decimal = Field(description="Serializado como string para não perder precisão")
    data_competencia: date
    descricao: str
    criado_em: datetime
    criado_por: str = Field(description="Identificador (sub) de quem registrou")
    estorno_de: UUID | None = None

    @classmethod
    def de(cls, lanc: Lancamento) -> "LancamentoSaida":
        return cls(
            id=lanc.id,
            tipo=lanc.tipo,
            valor=lanc.valor,
            data_competencia=lanc.data_competencia,
            descricao=lanc.descricao,
            criado_em=lanc.criado_em,
            criado_por=lanc.criado_por,
            estorno_de=lanc.estorno_de,
        )


class ListaLancamentos(BaseModel):
    itens: list[LancamentoSaida]
    limite: int
    deslocamento: int
