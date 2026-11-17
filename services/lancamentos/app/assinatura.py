"""Assinatura dos eventos publicados (Ed25519, assimétrica).

Só este serviço tem a chave PRIVADA. O Consolidado recebe apenas a chave PÚBLICA, que confere a
assinatura mas não consegue produzir uma. Assim nenhum outro componente consegue forjar um evento
aceito, nem quem comprometer o Consolidado, nem quem tiver a credencial do broker.

Formato do cabeçalho: `x-assinatura: ed25519=<base64 da assinatura do corpo exato publicado>`.
Chaves trafegam em base64 dos 32 bytes brutos (uma linha, cabe em variável de ambiente/Key Vault).

Gerar pares novos (imprime as linhas do .env): python -m app.gerar_chaves
"""

from __future__ import annotations

import base64
import binascii

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CABECALHO_ASSINATURA = "x-assinatura"
PREFIXO = "ed25519="


def carregar_chave_privada(texto_b64: str) -> Ed25519PrivateKey:
    """Lê a chave privada (base64 dos 32 bytes). Falha cedo se estiver ausente ou malformada."""
    try:
        bruto = base64.b64decode(texto_b64.strip(), validate=True)
        return Ed25519PrivateKey.from_private_bytes(bruto)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("chave privada Ed25519 inválida: esperado base64 de 32 bytes") from exc


def assinar(chave: Ed25519PrivateKey, corpo: bytes) -> str:
    return PREFIXO + base64.b64encode(chave.sign(corpo)).decode("ascii")


def _b64(bruto: bytes) -> str:
    return base64.b64encode(bruto).decode("ascii")


def gerar_par() -> tuple[str, str]:
    """Devolve (privada, pública) em base64 dos bytes brutos."""
    privada = Ed25519PrivateKey.generate()
    bytes_privada = privada.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    bytes_publica = privada.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64(bytes_privada), _b64(bytes_publica)
