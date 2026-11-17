"""Autenticação só com chave pública: o serviço confere tokens, mas não guarda segredo capaz de emitir."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import dev_token
from app.assinatura import carregar_chave_privada
from app.config import Settings
from app.gerar_chaves import linhas
from app.main import Dependencias, create_app
from conftest import IDP_PRIVADA_B64, IDP_PUBLICA_B64

URL = "/v1/lancamentos?data=2026-11-10"


def _token(chave, algoritmo: str) -> str:
    agora = int(time.time())
    claims = {
        "sub": "atacante",
        "iss": "fluxo-caixa-idp-local",
        "aud": "fluxo-caixa",
        "iat": agora,
        "exp": agora + 300,
        "comerciante_id": "loja-1",
        "scope": "lancamentos:read lancamentos:write",
    }
    return jwt.encode(claims, chave, algorithm=algoritmo)


def _get(client, token: str) -> int:
    return client.get(URL, headers={"Authorization": f"Bearer {token}"}).status_code


def test_token_emitido_pelo_idp_local_e_aceito(client):
    token = dev_token.emitir("loja-1", "lancamentos:read", chave_privada_b64=IDP_PRIVADA_B64)
    assert _get(client, token) == 200


def test_token_assinado_por_outra_chave_privada_e_recusado(client):
    assert _get(client, _token(Ed25519PrivateKey.generate(), "EdDSA")) == 401


def test_confusao_de_algoritmo_hs256_com_a_chave_publica_e_recusada(client):
    # Ataque clássico: a chave pública é conhecida, então o atacante a usa como "segredo" HMAC.
    assert _get(client, _token(IDP_PUBLICA_B64, "HS256")) == 401


def test_token_hs256_com_segredo_qualquer_e_recusado(client):
    assert _get(client, _token("segredo-compartilhado-antigo-com-mais-de-32-caracteres", "HS256")) == 401


def test_servico_nao_sobe_sem_chave_publica():
    with pytest.raises(RuntimeError, match="JWT_CHAVE_PUBLICA"):
        create_app(settings=Settings(outbox_relay_enabled=False))


def test_servico_nao_sobe_com_chave_publica_malformada():
    with pytest.raises(ValueError):
        create_app(settings=Settings(jwt_chave_publica="nao-e-base64!!", outbox_relay_enabled=False))


@pytest.mark.parametrize(
    "config",
    [
        {"jwt_chave_publica": IDP_PUBLICA_B64, "jwt_algorithm": "HS256"},
        {"jwt_jwks_url": "https://idp.exemplo/jwks", "jwt_algorithm": "HS256"},
        {"jwt_chave_publica": IDP_PUBLICA_B64, "jwt_algorithm": "none"},
    ],
)
def test_servico_nao_sobe_com_algoritmo_simetrico(config):
    with pytest.raises(RuntimeError, match="assimétrico"):
        create_app(settings=Settings(outbox_relay_enabled=False, **config))


def test_servico_nao_tem_configuracao_de_segredo_jwt():
    assert "jwt_secret" not in Settings.model_fields
    assert not any(c.startswith("jwt") and "privada" in c for c in Settings.model_fields)


def test_pares_gerados_sao_independentes_e_o_do_idp_funciona(repo):
    env = dict(l.split("=", 1) for l in linhas() if not l.startswith("#"))
    assert set(env) == {"EVENTOS_CHAVE_PRIVADA", "EVENTOS_CHAVE_PUBLICA", "IDP_CHAVE_PRIVADA", "IDP_CHAVE_PUBLICA"}
    assert env["EVENTOS_CHAVE_PRIVADA"] != env["IDP_CHAVE_PRIVADA"]
    carregar_chave_privada(env["EVENTOS_CHAVE_PRIVADA"])  # chave de eventos válida

    app = create_app(
        settings=Settings(jwt_chave_publica=env["IDP_CHAVE_PUBLICA"], outbox_relay_enabled=False),
        dependencias=Dependencias(repo=repo),
    )
    token = dev_token.emitir("loja-1", "lancamentos:read", chave_privada_b64=env["IDP_CHAVE_PRIVADA"])
    with TestClient(app) as c:
        assert _get(c, token) == 200
        # A chave de EVENTOS não serve para emitir token: pares separados por finalidade.
        assert _get(c, dev_token.emitir("loja-1", "lancamentos:read", chave_privada_b64=env["EVENTOS_CHAVE_PRIVADA"])) == 401
