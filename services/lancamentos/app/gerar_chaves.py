"""Gera os pares Ed25519 do ambiente local e imprime as linhas do .env.

  EVENTOS_*  assinatura dos eventos: privada só em Lançamentos, pública só no Consolidado.
  IDP_*      tokens do IdP local: privada só no emissor (idp-local e e2e), pública nos dois serviços.

Nenhum segredo de assinatura é compartilhado entre serviços.
Uso: python -m app.gerar_chaves   (ou `make env` / `make chaves`)
"""

from __future__ import annotations

from .assinatura import gerar_par


def linhas() -> list[str]:
    eventos_privada, eventos_publica = gerar_par()
    idp_privada, idp_publica = gerar_par()
    return [
        "# Eventos: privada só em Lançamentos, pública só no Consolidado",
        f"EVENTOS_CHAVE_PRIVADA={eventos_privada}",
        f"EVENTOS_CHAVE_PUBLICA={eventos_publica}",
        "# Tokens do IdP local: privada só no emissor (idp-local e e2e), pública nos dois serviços",
        f"IDP_CHAVE_PRIVADA={idp_privada}",
        f"IDP_CHAVE_PUBLICA={idp_publica}",
    ]


if __name__ == "__main__":  # pragma: no cover - utilitário de linha de comando
    print("\n".join(linhas()))
