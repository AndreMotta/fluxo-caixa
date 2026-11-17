from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from .security import Principal, exigir_escopo
from .service import ConsultaConsolidado

router = APIRouter(prefix="/v1/consolidado", tags=["consolidado diário"])


class RelatorioSaida(BaseModel):
    data: date
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_dia: Decimal = Field(description="Créditos - débitos do dia")
    saldo_acumulado: Decimal = Field(description="Saldo de fechamento: soma de todos os dias até a data")
    quantidade_lancamentos: int
    atualizado_em: datetime | None = Field(
        description="Último evento aplicado. O consolidado é eventualmente consistente (segundos)."
    )


def obter_consulta(request: Request) -> ConsultaConsolidado:
    return request.app.state.consulta


@router.get("/diario/{data}", response_model=RelatorioSaida)
async def consolidado_diario(
    data: date,
    response: Response,
    principal: Principal = Depends(exigir_escopo("consolidado:read")),
    consulta: ConsultaConsolidado = Depends(obter_consulta),
):
    rel = await consulta.relatorio(principal.comerciante_id, data)
    # Resposta por comerciante: proíbe cache compartilhado em proxies/CDN (evita vazamento entre tenants).
    response.headers["Cache-Control"] = "private, max-age=5"
    return RelatorioSaida(**rel.__dict__)
