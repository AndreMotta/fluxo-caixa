import json

from conftest import cabecalhos

URL = "/v1/lancamentos"


def test_exige_token(client):
    r = client.post(URL, json={"tipo": "CREDITO", "valor": "10.00"}, headers={"Idempotency-Key": "chave-0001"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_rejeita_token_com_assinatura_invalida(client):
    h = cabecalhos()
    h["Authorization"] = h["Authorization"][:-4] + "abcd"
    r = client.post(URL, json={"tipo": "CREDITO", "valor": "10.00"}, headers=h)
    assert r.status_code == 401


def test_exige_escopo_de_escrita(client):
    r = client.post(URL, json={"tipo": "CREDITO", "valor": "10.00"}, headers=cabecalhos(escopos="lancamentos:read"))
    assert r.status_code == 403


def test_exige_idempotency_key(client):
    r = client.post(URL, json={"tipo": "CREDITO", "valor": "10.00"}, headers=cabecalhos(chave=None))
    assert r.status_code == 422


def test_registra_credito_e_grava_evento_no_outbox(client, repo):
    r = client.post(URL, json={"tipo": "CREDITO", "valor": "150.75", "descricao": "venda balcão"}, headers=cabecalhos())
    assert r.status_code == 201, r.text
    corpo = r.json()
    assert corpo["valor"] == "150.75"
    assert corpo["data_competencia"] == "2026-11-10"  # padrão: hoje no fuso do comerciante
    assert r.headers["location"] == f"{URL}/{corpo['id']}"
    assert corpo["criado_por"] == "teste"  # `sub` do token: não-repúdio

    assert len(repo.outbox) == 1
    evento = json.loads(repo.outbox[0].payload)
    assert evento["lancamento_id"] == corpo["id"]
    assert evento["comerciante_id"] == "loja-1"
    assert "criado_por" not in evento  # minimização: identidade do usuário não trafega na fila


def test_escrita_gera_log_de_auditoria(client, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="auditoria")
    client.post(URL, json={"tipo": "CREDITO", "valor": "5"}, headers=cabecalhos("chave-audit-1"))
    registros = [r for r in caplog.records if r.name == "auditoria"]
    assert registros and registros[-1].campos["sujeito"] == "teste"
    assert registros[-1].campos["acao"] == "lancamento_registrado"


def test_recusa_gera_log_de_seguranca(client, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="seguranca")
    client.post(URL, json={"tipo": "CREDITO", "valor": "5"}, headers=cabecalhos(escopos="lancamentos:read"))
    assert any(r.campos["motivo"] == "escopo_insuficiente" for r in caplog.records if r.name == "seguranca")


def test_swagger_desligado_por_padrao(client):
    assert client.get("/lancamentos/docs").status_code == 404
    assert client.get("/lancamentos/openapi.json").status_code == 404


def test_reenvio_com_mesma_chave_nao_duplica(client, repo):
    corpo = {"tipo": "DEBITO", "valor": "20.00"}
    r1 = client.post(URL, json=corpo, headers=cabecalhos("chave-repetida"))
    r2 = client.post(URL, json=corpo, headers=cabecalhos("chave-repetida"))
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.headers["idempotent-replayed"] == "true"
    assert r1.json()["id"] == r2.json()["id"]
    assert len(repo.lancamentos) == 1 and len(repo.outbox) == 1


def test_reenvio_apos_a_meia_noite_sem_data_devolve_o_original(repo):
    # PDV sem rede reenvia no dia seguinte o mesmo pedido (sem data_competencia): é repetição, não conflito.
    from datetime import date

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import Dependencias, create_app
    from app.service import Relogio
    from conftest import AGORA, HOJE, IDP_PUBLICA_B64

    relogio = {"hoje": HOJE}
    app = create_app(
        settings=Settings(jwt_chave_publica=IDP_PUBLICA_B64, outbox_relay_enabled=False),
        dependencias=Dependencias(repo=repo, relogio=Relogio(agora=lambda: AGORA, hoje=lambda: relogio["hoje"])),
    )
    corpo = {"tipo": "CREDITO", "valor": "20.00"}
    with TestClient(app) as c:
        r1 = c.post(URL, json=corpo, headers=cabecalhos("chave-virada-1"))
        relogio["hoje"] = date(2026, 11, 11)
        r2 = c.post(URL, json=corpo, headers=cabecalhos("chave-virada-1"))
    assert r1.status_code == 201
    assert r2.status_code == 200 and r2.headers["idempotent-replayed"] == "true"
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["data_competencia"] == "2026-11-10"  # vale a data do registro original
    assert len(repo.lancamentos) == 1 and len(repo.outbox) == 1


def test_mesma_chave_com_outra_data_explicita_retorna_409(client):
    client.post(URL, json={"tipo": "DEBITO", "valor": "20.00", "data_competencia": "2026-11-09"}, headers=cabecalhos("chave-data-01"))
    r = client.post(URL, json={"tipo": "DEBITO", "valor": "20.00", "data_competencia": "2026-11-10"}, headers=cabecalhos("chave-data-01"))
    assert r.status_code == 409


def test_mesma_chave_com_outro_conteudo_retorna_409(client):
    client.post(URL, json={"tipo": "DEBITO", "valor": "20.00"}, headers=cabecalhos("chave-xyz-01"))
    r = client.post(URL, json={"tipo": "DEBITO", "valor": "21.00"}, headers=cabecalhos("chave-xyz-01"))
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/problem+json")


def test_validacoes_de_entrada(client):
    h = cabecalhos()
    assert client.post(URL, json={"tipo": "CREDITO", "valor": "1.234"}, headers=h).status_code == 422
    assert client.post(URL, json={"tipo": "CREDITO", "valor": "-5"}, headers=h).status_code == 422
    assert client.post(URL, json={"tipo": "PIX", "valor": "5"}, headers=h).status_code == 422
    assert client.post(URL, json={"tipo": "CREDITO", "valor": "5", "comerciante_id": "x"}, headers=h).status_code == 422


def test_data_futura_viola_regra_de_negocio(client):
    r = client.post(URL, json={"tipo": "CREDITO", "valor": "5", "data_competencia": "2026-11-11"}, headers=cabecalhos())
    assert r.status_code == 422
    assert r.json()["title"] == "Regra de negócio violada"


def test_comerciante_nao_enxerga_dados_de_outro(client):
    criado = client.post(URL, json={"tipo": "CREDITO", "valor": "5"}, headers=cabecalhos()).json()
    outro = cabecalhos(chave=None, comerciante="loja-2")
    assert client.get(f"{URL}/{criado['id']}", headers=outro).status_code == 404
    assert client.get(URL, params={"data": "2026-11-10"}, headers=outro).json()["itens"] == []
    assert len(client.get(URL, params={"data": "2026-11-10"}, headers=cabecalhos(chave=None)).json()["itens"]) == 1


def test_estorno(client):
    original = client.post(URL, json={"tipo": "DEBITO", "valor": "100.00"}, headers=cabecalhos("chave-orig-1")).json()

    r = client.post(f"{URL}/{original['id']}/estorno", json={"motivo": "duplicado"}, headers=cabecalhos("chave-est-01"))
    assert r.status_code == 201, r.text
    estorno = r.json()
    assert estorno["tipo"] == "CREDITO" and estorno["valor"] == "100.00"
    assert estorno["estorno_de"] == original["id"]

    # segundo estorno do mesmo lançamento, com outra chave: conflito
    assert client.post(f"{URL}/{original['id']}/estorno", headers=cabecalhos("chave-est-02")).status_code == 409
    # estorno de estorno: regra de negócio
    assert client.post(f"{URL}/{estorno['id']}/estorno", headers=cabecalhos("chave-est-03")).status_code == 422


def test_estorno_de_lancamento_inexistente(client):
    r = client.post(f"{URL}/00000000-0000-0000-0000-000000000000/estorno", headers=cabecalhos("chave-est-04"))
    assert r.status_code == 404


def test_health_e_metricas(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert "lancamentos_registrados_total" in client.get("/metrics").text
