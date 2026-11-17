"""Autenticação e autorização (OAuth2 Bearer / JWT).

Mesmo contrato do serviço de Lançamentos: comerciante vem da claim `comerciante_id`,
escopo exigido por operação (consolidado:read), só algoritmo assimétrico (o serviço tem
apenas a chave PÚBLICA do IdP e não consegue emitir token) e log de segurança em toda
recusa. Em uma organização maior isto vira uma biblioteca interna compartilhada.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .observability import ACESSOS_NEGADOS

_bearer = HTTPBearer(auto_error=False)
_log_seguranca = logging.getLogger("seguranca")

_PREFIXOS_ASSIMETRICOS = ("RS", "PS", "ES", "EdDSA")


@dataclass(frozen=True)
class Principal:
    sujeito: str
    comerciante_id: str
    escopos: frozenset[str]


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


@lru_cache(maxsize=4)
def _chave_publica_local(texto_b64: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(texto_b64.strip(), validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("chave pública do IdP inválida: esperado base64 de 32 bytes (Ed25519)") from exc


def validar_configuracao(settings: Settings) -> None:
    """Fail fast na subida: sem algoritmo assimétrico e chave pública (ou JWKS) válidos, não sobe."""
    prefixo = settings.model_config.get("env_prefix", "")
    if not settings.jwt_algorithm.startswith(_PREFIXOS_ASSIMETRICOS):
        raise RuntimeError(f"{prefixo}JWT_ALGORITHM deve ser assimétrico (EdDSA, RS256, ES256...)")
    if settings.jwt_jwks_url:
        return
    if not settings.jwt_chave_publica:
        raise RuntimeError(f"configure {prefixo}JWT_CHAVE_PUBLICA ou {prefixo}JWT_JWKS_URL")
    if settings.jwt_algorithm != "EdDSA":
        raise RuntimeError(f"a chave pública local é Ed25519: use {prefixo}JWT_ALGORITHM=EdDSA")
    _chave_publica_local(settings.jwt_chave_publica)


def _chave(settings: Settings, token: str):
    if settings.jwt_jwks_url:
        return _jwks_client(settings.jwt_jwks_url).get_signing_key_from_jwt(token).key
    return _chave_publica_local(settings.jwt_chave_publica)


def _recusar(codigo: int, motivo: str, request: Request, sujeito: str = "-") -> HTTPException:
    ACESSOS_NEGADOS.labels(motivo).inc()
    _log_seguranca.warning(
        "acesso negado",
        extra={
            "campos": {
                "evento": "acesso_negado",
                "motivo": motivo,
                "sujeito": sujeito,
                "rota": request.url.path,
                "origem": request.client.host if request.client else "-",
            }
        },
    )
    cabecalhos = {"WWW-Authenticate": "Bearer"} if codigo == status.HTTP_401_UNAUTHORIZED else None
    return HTTPException(status_code=codigo, detail=motivo.replace("_", " "), headers=cabecalhos)


def validar_token(token: str, settings: Settings, request: Request) -> Principal:
    try:
        claims = jwt.decode(
            token,
            _chave(settings, token),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            leeway=10,
        )
    except jwt.PyJWTError as exc:
        raise _recusar(status.HTTP_401_UNAUTHORIZED, "token_invalido", request) from exc
    sujeito = str(claims["sub"])
    comerciante = claims.get("comerciante_id")
    if not isinstance(comerciante, str) or not comerciante:
        raise _recusar(status.HTTP_403_FORBIDDEN, "token_sem_comerciante", request, sujeito)
    escopos = frozenset(str(claims.get("scope", "")).split())
    return Principal(sujeito=sujeito, comerciante_id=comerciante, escopos=escopos)


def exigir_escopo(escopo: str):
    def dependencia(
        request: Request,
        credenciais: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> Principal:
        if credenciais is None:
            raise _recusar(status.HTTP_401_UNAUTHORIZED, "token_ausente", request)
        principal = validar_token(credenciais.credentials, request.app.state.settings, request)
        if escopo not in principal.escopos:
            raise _recusar(status.HTTP_403_FORBIDDEN, "escopo_insuficiente", request, principal.sujeito)
        return principal

    return dependencia
