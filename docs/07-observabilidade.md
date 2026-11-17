# 7. Monitoramento e observabilidade

## 7.1 O que observar (e por quê)

Num sistema com consistência eventual, "a API respondeu 200" não basta. É preciso saber **quanto o consolidado
está atrasado** e **se há eventos parados**. Por isso, além das métricas RED (rate, errors, duration),
o sistema expõe métricas do fluxo de negócio.

| Sinal | Métrica (Prometheus) | Implementado | Uso |
|---|---|---|---|
| Taxa, erro e latência HTTP | `http_requests_total{service,route,status}`, `http_request_duration_seconds` | ✅ | SLOs de disponibilidade e latência |
| Lançamentos registrados | `lancamentos_registrados_total{tipo}` | ✅ | Volume de negócio; anomalia (queda brusca = PDVs sem conseguir lançar) |
| Backlog do outbox | `outbox_eventos_pendentes` | ✅ | Broker fora ou lento |
| Falhas de publicação | `outbox_falhas_publicacao_total` | ✅ | Saúde do broker visto pelo produtor |
| Eventos consumidos | `consolidado_eventos_total{resultado=aplicado,duplicado,invalido,assinatura_invalida,banco_indisponivel,erro}` | ✅ | Duplicidade, mensagens envenenadas e **adulteração** (alerta de segurança) |
| Acessos negados | `acessos_negados_total{motivo}` | ✅ | Força bruta de token, varredura de escopos |
| **Defasagem** lançamento → consolidado | `consolidado_defasagem_segundos` | ✅ | SLO de consistência (p95 < 5 s) |
| Cache | `consolidado_cache_total{resultado=hit,miss,erro}` | ✅ | Eficiência e degradação |
| Fila e DLQ | `rabbitmq_queue_messages_ready{queue}` | ✅ (scrape) | Mensagens paradas, DLQ > 0 |

## 7.2 SLOs e alertas

Definidos em [`infra/prometheus/alertas.yml`](../infra/prometheus/alertas.yml):

| SLO | Meta | Alerta |
|---|---|---|
| Disponibilidade Lançamentos | 99,9% (erro 5xx < 0,1%) | `LancamentosTaxaDeErroAlta` (page) |
| Disponibilidade Consolidado no pico | perda ≤ 5% (meta interna ≤ 1%) | `ConsolidadoTaxaDeErroAlta` (page) |
| Latência Consolidado | p95 < 200 ms | `ConsolidadoLatenciaP95Alta` |
| Consistência | defasagem p95 < 5 s | `ConsolidadoDefasado` |
| Integridade | DLQ vazia | `MensagensNaDLQ` |
| Fluxo | outbox < 1000 pendentes | `OutboxAcumulando` |

Em produção eu usaria alertas de **burn rate** em múltiplas janelas (1 h/5 min e 6 h/30 min) no lugar de limiar fixo,
para reduzir falso positivo.

## 7.3 Logs

- ✅ JSON em stdout (`ts`, `level`, `service`, `request_id`, `msg`), prontos para Log Analytics/Loki.
- ✅ `X-Request-ID` gerado no gateway, propagado aos serviços e devolvido ao cliente: o suporte pede o ID e encontra a requisição.
- ✅ Access log do gateway em JSON com tempo de upstream.
- Regras: nunca logar token, corpo inteiro de requisição nem segredo; `comerciante_id` pseudonimizado quando for pessoa física.

## 7.4 Tracing (evolução)

Instrumentar com **OpenTelemetry** (FastAPI, psycopg, aio-pika) e propagar o `traceparent` **dentro do evento**
(header da mensagem). Assim um trace mostra o caminho inteiro: `POST` → commit → relay → broker → consumidor → `UPSERT`.
Exportação via OTel Collector para Azure Monitor/App Insights (ou Tempo/Jaeger).

## 7.5 Dashboards sugeridos

1. **Visão do negócio:** lançamentos/min por tipo, valor movimentado, comerciantes ativos.
2. **Golden signals por serviço:** RPS, % erro, p50/p95/p99, saturação (CPU, conexões do pool).
3. **Fluxo assíncrono:** backlog do outbox, profundidade da fila, defasagem p95, DLQ, duplicados.
4. **Dependências:** latência e erro do Postgres, Redis (hit ratio) e broker.

## 7.6 Como rodar local

```bash
make observabilidade        # Prometheus em http://localhost:9090
# Consultas úteis:
#   sum by (route,status) (rate(http_requests_total[1m]))
#   histogram_quantile(0.95, sum by (le) (rate(consolidado_defasagem_segundos_bucket[5m])))
#   max(outbox_eventos_pendentes)
```
