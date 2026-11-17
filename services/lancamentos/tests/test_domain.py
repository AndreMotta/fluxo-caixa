import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.domain import (
    EventoLancamentoRegistrado,
    Lancamento,
    RegraDeNegocioViolada,
    TipoLancamento,
    hash_requisicao,
)

HOJE = date(2026, 11, 10)
AGORA = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def novo(**kw) -> Lancamento:
    base = dict(
        comerciante_id="loja-1",
        tipo=TipoLancamento.CREDITO,
        valor=Decimal("10.00"),
        data_competencia=HOJE,
        descricao="venda",
        hoje=HOJE,
        agora=AGORA,
        criado_por="usuario-1",
    )
    base.update(kw)
    return Lancamento.novo(**base)


def test_registra_autor_e_exige_autor():
    assert novo().criado_por == "usuario-1"
    with pytest.raises(RegraDeNegocioViolada):
        novo(criado_por="")


def test_normaliza_valor_para_duas_casas():
    assert novo(valor=Decimal("10")).valor == Decimal("10.00")


@pytest.mark.parametrize("valor", [Decimal("0"), Decimal("-1.00"), Decimal("1.001"), Decimal("1000000000000.00")])
def test_rejeita_valores_invalidos(valor):
    with pytest.raises(RegraDeNegocioViolada):
        novo(valor=valor)


def test_rejeita_data_futura():
    with pytest.raises(RegraDeNegocioViolada):
        novo(data_competencia=date(2026, 11, 11))


def test_aceita_data_retroativa():
    assert novo(data_competencia=date(2026, 10, 1)).data_competencia == date(2026, 10, 1)


def test_estorno_inverte_tipo_e_usa_data_de_hoje():
    original = novo(tipo=TipoLancamento.DEBITO, valor=Decimal("99.90"), data_competencia=date(2026, 11, 1))
    estorno = original.estornar(hoje=HOJE, agora=AGORA, criado_por="gerente-2", motivo="cobrança duplicada")
    assert estorno.criado_por == "gerente-2"  # quem estornou, não quem lançou
    assert estorno.tipo is TipoLancamento.CREDITO
    assert estorno.valor == Decimal("99.90")
    assert estorno.data_competencia == HOJE
    assert estorno.estorno_de == original.id
    assert "cobrança duplicada" in estorno.descricao


def test_nao_permite_estornar_um_estorno():
    estorno = novo().estornar(hoje=HOJE, agora=AGORA, criado_por="usuario-1")
    with pytest.raises(RegraDeNegocioViolada):
        estorno.estornar(hoje=HOJE, agora=AGORA, criado_por="usuario-1")


def test_evento_de_integracao_tem_contrato_versionado():
    lanc = novo(valor=Decimal("1.50"))
    payload = json.loads(EventoLancamentoRegistrado(lanc).to_json())
    assert payload["event_type"] == "LancamentoRegistrado"
    assert payload["event_version"] == 1
    assert payload["valor"] == "1.50"  # string: sem perda de precisão
    assert payload["tipo"] == "CREDITO"
    assert payload["data_competencia"] == "2026-11-10"
    assert payload["comerciante_id"] == "loja-1"


def test_hash_requisicao_e_deterministico():
    assert hash_requisicao({"a": 1, "b": 2}) == hash_requisicao({"b": 2, "a": 1})
    assert hash_requisicao({"a": 1}) != hash_requisicao({"a": 2})
