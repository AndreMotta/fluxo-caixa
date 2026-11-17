"""Teste ponta a ponta contra o ambiente do docker compose (passando pelo gateway).

Roda com: make e2e  (container no profile `e2e`, na mesma rede do gateway)
"""

import base64
import os
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE = os.environ.get("BASE_URL", "http://localhost:8080")
CHAVE_PRIVADA_B64 = os.environ.get("IDP_CHAVE_PRIVADA")

pytestmark = pytest.mark.skipif(not CHAVE_PRIVADA_B64, reason="defina IDP_CHAVE_PRIVADA para rodar o e2e")


def _chave_idp() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(CHAVE_PRIVADA_B64))


def _token(comerciante: str, chave: Ed25519PrivateKey | None = None) -> str:
    agora = int(time.time())
    return jwt.encode(
        {
            "sub": "e2e",
            "iss": "fluxo-caixa-idp-local",
            "aud": "fluxo-caixa",
            "iat": agora,
            "exp": agora + 300,
            "comerciante_id": comerciante,
            "scope": "lancamentos:write lancamentos:read consolidado:read",
        },
        chave or _chave_idp(),
        algorithm="EdDSA",
    )


@pytest.fixture
def api():
    comerciante = f"e2e-{uuid.uuid4().hex[:10]}"  # comerciante novo a cada execução
    with httpx.Client(base_url=BASE, timeout=5, headers={"Authorization": f"Bearer {_token(comerciante)}"}) as c:
        yield c


def _lancar(api, tipo: str, valor: str, chave: str | None = None) -> httpx.Response:
    return api.post(
        "/v1/lancamentos",
        json={"tipo": tipo, "valor": valor, "descricao": "e2e"},
        headers={"Idempotency-Key": chave or uuid.uuid4().hex},
    )


def test_lancamentos_refletem_no_consolidado(api):
    r1 = _lancar(api, "CREDITO", "100.00")
    r2 = _lancar(api, "DEBITO", "30.00")
    assert r1.status_code == 201 and r2.status_code == 201
    dia = r1.json()["data_competencia"]

    corpo = {}
    prazo = time.time() + 30
    while time.time() < prazo:
        corpo = api.get(f"/v1/consolidado/diario/{dia}").json()
        if corpo.get("saldo_dia") == "70.00":
            break
        time.sleep(0.5)
    assert corpo["total_creditos"] == "100.00"
    assert corpo["total_debitos"] == "30.00"
    assert corpo["saldo_dia"] == "70.00"
    assert corpo["quantidade_lancamentos"] == 2


def test_reenvio_pelo_gateway_e_idempotente(api):
    a = _lancar(api, "CREDITO", "1.00", chave="e2e-chave-fixa")
    b = _lancar(api, "CREDITO", "1.00", chave="e2e-chave-fixa")
    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_borda_nao_expoe_rotas_internas():
    with httpx.Client(base_url=BASE, timeout=5) as c:
        assert c.get("/metrics").status_code == 404
        assert c.get("/health/ready").status_code == 404
        assert c.get("/v1/consolidado/diario/2026-01-01").status_code == 401


def test_token_de_outra_chave_e_recusado_nos_dois_servicos():
    falso = _token("e2e-intruso", chave=Ed25519PrivateKey.generate())
    with httpx.Client(base_url=BASE, timeout=5, headers={"Authorization": f"Bearer {falso}"}) as c:
        assert c.get("/v1/consolidado/diario/2026-01-01").status_code == 401
        assert c.get("/v1/lancamentos?data=2026-01-01").status_code == 401
