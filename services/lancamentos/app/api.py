from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from .schemas import EstornoEntrada, LancamentoEntrada, LancamentoSaida, ListaLancamentos
from .security import Principal, exigir_escopo
from .service import ServicoLancamentos

router = APIRouter(prefix="/v1/lancamentos", tags=["lançamentos"])

ChaveIdempotencia = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        description="Obrigatório. Reenvios com a mesma chave não duplicam o lançamento.",
    ),
]


def obter_servico(request: Request) -> ServicoLancamentos:
    return request.app.state.servico


def _responder_registro(resultado, response: Response) -> LancamentoSaida:
    if not resultado.criado:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    response.headers["Location"] = f"/v1/lancamentos/{resultado.lancamento.id}"
    return LancamentoSaida.de(resultado.lancamento)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=LancamentoSaida)
async def registrar_lancamento(
    corpo: LancamentoEntrada,
    response: Response,
    idempotency_key: ChaveIdempotencia,
    principal: Principal = Depends(exigir_escopo("lancamentos:write")),
    servico: ServicoLancamentos = Depends(obter_servico),
):
    resultado = await servico.registrar(
        comerciante_id=principal.comerciante_id,
        tipo=corpo.tipo,
        valor=corpo.valor,
        data_competencia=corpo.data_competencia,
        descricao=corpo.descricao,
        idempotency_key=idempotency_key,
        criado_por=principal.sujeito,
    )
    return _responder_registro(resultado, response)


@router.post(
    "/{lancamento_id}/estorno",
    status_code=status.HTTP_201_CREATED,
    response_model=LancamentoSaida,
)
async def estornar_lancamento(
    lancamento_id: UUID,
    response: Response,
    idempotency_key: ChaveIdempotencia,
    corpo: EstornoEntrada | None = None,
    principal: Principal = Depends(exigir_escopo("lancamentos:write")),
    servico: ServicoLancamentos = Depends(obter_servico),
):
    resultado = await servico.estornar(
        comerciante_id=principal.comerciante_id,
        lancamento_id=lancamento_id,
        motivo=(corpo.motivo if corpo else ""),
        idempotency_key=idempotency_key,
        criado_por=principal.sujeito,
    )
    return _responder_registro(resultado, response)


@router.get("/{lancamento_id}", response_model=LancamentoSaida)
async def obter_lancamento(
    lancamento_id: UUID,
    principal: Principal = Depends(exigir_escopo("lancamentos:read")),
    servico: ServicoLancamentos = Depends(obter_servico),
):
    return LancamentoSaida.de(await servico.obter(principal.comerciante_id, lancamento_id))


@router.get("", response_model=ListaLancamentos)
async def listar_lancamentos(
    data: date,
    limite: Annotated[int, Query(ge=1, le=500)] = 100,
    deslocamento: Annotated[int, Query(ge=0, le=100_000)] = 0,
    principal: Principal = Depends(exigir_escopo("lancamentos:read")),
    servico: ServicoLancamentos = Depends(obter_servico),
):
    itens = await servico.listar(principal.comerciante_id, data, limite, deslocamento)
    return ListaLancamentos(
        itens=[LancamentoSaida.de(i) for i in itens], limite=limite, deslocamento=deslocamento
    )
