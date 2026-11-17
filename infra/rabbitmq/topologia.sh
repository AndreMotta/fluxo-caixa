#!/bin/sh
# Topologia e usuários do broker como código (em nuvem: Bicep/Terraform do Service Bus).
#
# Privilégio mínimo (regex de permissão do RabbitMQ: configure / write / read):
#   fluxo_lancamentos: só PUBLICA no exchange fluxo-caixa.lancamentos
#   fluxo_consolidado: só CONSOME a fila consolidado.lancamentos
# Nenhum serviço consegue criar/apagar filas, publicar no lugar do outro ou ler a DLQ.
# Idempotente: pode rodar a cada `docker compose up`.
set -eu

API="http://rabbitmq:15672/api"
AUTH="${RABBITMQ_USER}:${RABBITMQ_PASSWORD}"
VH="%2F"

api() { # método caminho [json]
  if [ $# -ge 3 ]; then
    curl -sS --fail-with-body -u "$AUTH" -H "content-type: application/json" -X "$1" "$API$2" -d "$3"
  else
    curl -sS --fail-with-body -u "$AUTH" -X "$1" "$API$2"
  fi
  echo
}

i=0
until curl -sf -u "$AUTH" "$API/overview" >/dev/null; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && { echo "API de gestão do RabbitMQ indisponível"; exit 1; }
  sleep 2
done

echo "== topologia =="
api PUT "/exchanges/$VH/fluxo-caixa.lancamentos" '{"type":"topic","durable":true}'
api PUT "/exchanges/$VH/consolidado.lancamentos.dlx" '{"type":"fanout","durable":true}'
api PUT "/queues/$VH/consolidado.lancamentos.dlq" '{"durable":true,"arguments":{"x-queue-type":"quorum"}}'
api POST "/bindings/$VH/e/consolidado.lancamentos.dlx/q/consolidado.lancamentos.dlq" '{"routing_key":""}'
api PUT "/queues/$VH/consolidado.lancamentos" \
  '{"durable":true,"arguments":{"x-queue-type":"quorum","x-delivery-limit":10,"x-dead-letter-exchange":"consolidado.lancamentos.dlx"}}'
api POST "/bindings/$VH/e/fluxo-caixa.lancamentos/q/consolidado.lancamentos" '{"routing_key":"lancamento.registrado.*"}'

echo "== usuários de serviço =="
api PUT "/users/fluxo_lancamentos" "{\"password\":\"${RABBITMQ_LANC_PASSWORD}\",\"tags\":\"\"}"
api PUT "/permissions/$VH/fluxo_lancamentos" '{"configure":"^$","write":"^fluxo-caixa\\.lancamentos$","read":"^$"}'

api PUT "/users/fluxo_consolidado" "{\"password\":\"${RABBITMQ_CONS_PASSWORD}\",\"tags\":\"\"}"
api PUT "/permissions/$VH/fluxo_consolidado" '{"configure":"^$","write":"^$","read":"^consolidado\\.lancamentos$"}'

echo "topologia e permissões aplicadas"
