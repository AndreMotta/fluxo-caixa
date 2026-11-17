import asyncio

from app.service import ProjecaoSaldo
from conftest import CacheEmMemoria, auth, evento

URL = "/v1/consolidado/diario"


def _popular(repo):
    p = ProjecaoSaldo(repo)
    for kw in [
        dict(tipo="CREDITO", valor="200.00", data="2026-11-09"),
        dict(tipo="CREDITO", valor="150.75", data="2026-11-10"),
        dict(tipo="DEBITO", valor="50.25", data="2026-11-10"),
        dict(tipo="CREDITO", valor="999.00", data="2026-11-10", comerciante="loja-2"),
    ]:
        asyncio.run(p.aplicar(evento(**kw)))


def test_exige_token_e_escopo(client):
    assert client.get(f"{URL}/2026-11-10").status_code == 401
    assert client.get(f"{URL}/2026-11-10", headers=auth(escopos="lancamentos:read")).status_code == 403


def test_consolidado_do_dia(client, repo):
    _popular(repo)
    r = client.get(f"{URL}/2026-11-10", headers=auth())
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["total_creditos"] == "150.75"
    assert corpo["total_debitos"] == "50.25"
    assert corpo["saldo_dia"] == "100.50"
    assert corpo["saldo_acumulado"] == "300.50"
    assert corpo["quantidade_lancamentos"] == 2
    assert r.headers["cache-control"].startswith("private")


def test_dia_sem_movimento_retorna_zeros_e_saldo_anterior(client, repo):
    _popular(repo)
    corpo = client.get(f"{URL}/2026-11-11", headers=auth()).json()
    assert corpo["saldo_dia"] == "0.00"
    assert corpo["saldo_acumulado"] == "300.50"
    assert corpo["atualizado_em"] is None


def test_comerciante_so_ve_o_proprio_consolidado(client, repo):
    _popular(repo)
    corpo = client.get(f"{URL}/2026-11-10", headers=auth(comerciante="loja-2")).json()
    assert corpo["total_creditos"] == "999.00"


def test_segunda_consulta_vem_do_cache(client, repo, cache):
    _popular(repo)
    client.get(f"{URL}/2026-11-10", headers=auth())
    client.get(f"{URL}/2026-11-10", headers=auth())
    assert repo.consultas == 1
    assert "consolidado:v1:loja-1:2026-11-10" in cache.dados


def test_cache_fora_do_ar_nao_derruba_consulta(repo):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import Dependencias, create_app
    from conftest import IDP_PUBLICA_B64

    app = create_app(
        settings=Settings(jwt_chave_publica=IDP_PUBLICA_B64, consumidor_enabled=False),
        dependencias=Dependencias(repo=repo, cache=CacheEmMemoria(quebrado=True)),
    )
    with TestClient(app) as c:
        assert c.get(f"{URL}/2026-11-10", headers=auth()).status_code == 200
        assert c.get(f"{URL}/2026-11-10", headers=auth()).status_code == 200
    assert repo.consultas == 2


def test_data_invalida(client):
    assert client.get(f"{URL}/10-11-2026", headers=auth()).status_code == 422


def test_health_e_metricas(client):
    assert client.get("/health/ready").status_code == 200
    assert "consolidado_eventos_total" in client.get("/metrics").text


def test_swagger_desligado_por_padrao(client):
    assert client.get("/consolidado/docs").status_code == 404
    assert client.get("/consolidado/openapi.json").status_code == 404


def test_token_com_alg_none_e_recusado(client):
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    forjado = b64({"alg": "none", "typ": "JWT"}) + "." + b64(
        {"sub": "x", "iss": "fluxo-caixa-idp-local", "aud": "fluxo-caixa", "iat": 1, "exp": 9999999999,
         "comerciante_id": "loja-1", "scope": "consolidado:read"}
    ) + "."
    assert client.get(f"{URL}/2026-11-10", headers={"Authorization": f"Bearer {forjado}"}).status_code == 401


def _token(chave, algoritmo: str) -> str:
    import time

    import jwt

    agora = int(time.time())
    claims = {"sub": "atacante", "iss": "fluxo-caixa-idp-local", "aud": "fluxo-caixa", "iat": agora,
              "exp": agora + 300, "comerciante_id": "loja-1", "scope": "consolidado:read"}
    return jwt.encode(claims, chave, algorithm=algoritmo)


def test_token_assinado_por_outra_chave_privada_e_recusado(client):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    token = _token(Ed25519PrivateKey.generate(), "EdDSA")
    assert client.get(f"{URL}/2026-11-10", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_confusao_de_algoritmo_hs256_com_a_chave_publica_e_recusada(client):
    from conftest import IDP_PUBLICA_B64

    token = _token(IDP_PUBLICA_B64, "HS256")
    assert client.get(f"{URL}/2026-11-10", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_servico_nao_sobe_sem_chave_publica_nem_com_algoritmo_simetrico():
    import pytest

    from app.config import Settings
    from app.main import create_app
    from conftest import IDP_PUBLICA_B64

    assert "jwt_secret" not in Settings.model_fields
    with pytest.raises(RuntimeError, match="JWT_CHAVE_PUBLICA"):
        create_app(settings=Settings(consumidor_enabled=False))
    with pytest.raises(RuntimeError, match="assimétrico"):
        create_app(settings=Settings(jwt_chave_publica=IDP_PUBLICA_B64, jwt_algorithm="HS256", consumidor_enabled=False))
