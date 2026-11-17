#!/usr/bin/env bash
# Emite um JWT pelo IdP local (container idp-local: sem rede, único que recebe a chave privada).
# Imprime SÓ o token, pronto para ir num header HTTP: qualquer outra linha que o docker compose
# escreva na saída (progresso de build/criação do container) é descartada.
#
# Uso: bash scripts/token.sh --comerciante loja-1 [--escopos "..."] [--minutos 60]
set -euo pipefail

saida="$(docker compose --profile ferramentas run --rm -T idp-local "$@" | tr -d '\r')"
token="$(printf '%s\n' "$saida" | grep -E '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$' | tail -n 1 || true)"

if [ -z "$token" ]; then
  echo "erro: o idp-local não devolveu um token. Saída recebida:" >&2
  printf '%s\n' "$saida" >&2
  exit 1
fi

linhas="$(printf '%s\n' "$saida" | grep -c . || true)"
if [ "$linhas" -gt 1 ]; then
  echo "aviso: a saída do docker compose trouxe $((linhas - 1)) linha(s) além do token; usando só o token" >&2
fi

printf '%s\n' "$token"
