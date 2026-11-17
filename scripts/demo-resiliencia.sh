#!/usr/bin/env bash
# Demonstra o requisito: "o controle de lançamentos não fica indisponível se o consolidado cair".
# Derruba consolidado + broker, registra lançamentos, sobe de volta e mostra que nada se perdeu.
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8080}"
COMERCIANTE="demo-$(date +%s)"
# Token emitido pelo IdP local (container sem rede; os serviços só têm a chave pública).
TOKEN="$(bash "$(dirname "$0")/token.sh" --comerciante "$COMERCIANTE")"
HOJE="$(TZ=America/Sao_Paulo date +%F)"

echo "0) Conferindo o token com tudo no ar..."
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/lancamentos?data=$HOJE" -H "Authorization: Bearer $TOKEN")
if [ "$code" != "200" ]; then
  echo "   FALHOU: GET /v1/lancamentos devolveu HTTP $code (esperado 200)."
  echo "   O problema está no token ou no ambiente, não na resiliência. Nada foi derrubado."
  exit 1
fi
echo "   token aceito"

echo "1) Derrubando consolidado e rabbitmq..."
docker compose stop consolidado rabbitmq >/dev/null

echo "2) Registrando 5 créditos de 10,00 com o consolidado FORA do ar:"
for i in 1 2 3 4 5; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/lancamentos" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -H "Idempotency-Key: $COMERCIANTE-$i" -d '{"tipo":"CREDITO","valor":"10.00"}')
  echo "   lançamento $i -> HTTP $code"
  if [ "$code" != "201" ]; then
    echo "   FALHOU: lançamentos deveria responder 201 com o consolidado e o broker fora do ar"
    docker compose start rabbitmq consolidado >/dev/null
    exit 1
  fi
done
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/consolidado/diario/$HOJE" -H "Authorization: Bearer $TOKEN")
echo "   consulta ao consolidado agora -> HTTP $code (esperado 502: só ele está fora)"

echo "3) Subindo rabbitmq e consolidado de volta..."
docker compose start rabbitmq consolidado >/dev/null

echo "4) Aguardando o consolidado processar o backlog do outbox..."
for _ in $(seq 1 90); do
  corpo=$(curl -s "$BASE/v1/consolidado/diario/$HOJE" -H "Authorization: Bearer $TOKEN" || true)
  if echo "$corpo" | grep -q '"saldo_dia":"50.00"'; then
    echo "   OK: $corpo"
    exit 0
  fi
  sleep 2
done
echo "   FALHOU: consolidado não chegou a 50.00. Última resposta: ${corpo:-vazia}"
exit 1
