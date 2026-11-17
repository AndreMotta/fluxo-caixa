"""IdP LOCAL de desenvolvimento: emite um JWT assinado com Ed25519 (EdDSA).

É o ÚNICO componente que recebe a chave privada dos tokens (`IDP_CHAVE_PRIVADA`). Os serviços
recebem só a pública: conferem tokens, mas não conseguem emitir. No compose roda no container
`idp-local`, sob demanda (`make token`), sem rede e separado dos serviços em execução.
Em produção os tokens vêm do IdP corporativo (Entra ID: RS256 + JWKS).

Uso: python -m app.dev_token --comerciante loja-1 --escopos "lancamentos:write lancamentos:read consolidado:read"
"""

from __future__ import annotations

import argparse
import os
import time

import jwt

from .assinatura import carregar_chave_privada

EMISSOR = os.environ.get("IDP_EMISSOR", "fluxo-caixa-idp-local")
AUDIENCIA = os.environ.get("IDP_AUDIENCIA", "fluxo-caixa")


def emitir(
    comerciante: str,
    escopos: str,
    minutos: int = 60,
    sujeito: str = "dev-user",
    chave_privada_b64: str | None = None,
) -> str:
    chave_b64 = chave_privada_b64 or os.environ.get("IDP_CHAVE_PRIVADA")
    if not chave_b64:
        raise SystemExit("IDP_CHAVE_PRIVADA não configurada (gere com make env ou make chaves)")
    agora = int(time.time())
    claims = {
        "sub": sujeito,
        "iss": EMISSOR,
        "aud": AUDIENCIA,
        "iat": agora,
        "exp": agora + minutos * 60,
        "comerciante_id": comerciante,
        "scope": escopos,
    }
    return jwt.encode(claims, carregar_chave_privada(chave_b64), algorithm="EdDSA")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--comerciante", default="loja-1")
    p.add_argument("--escopos", default="lancamentos:write lancamentos:read consolidado:read")
    p.add_argument("--minutos", type=int, default=60)
    a = p.parse_args()
    print(emitir(a.comerciante, a.escopos, a.minutos))
