# 9. O que eu faria com mais tempo (evoluções)

Priorizado por valor/risco.

## Curto prazo (próximas sprints)

1. **Testes de integração com Testcontainers** (Postgres e RabbitMQ reais) para os repositórios e o relay.
   Hoje os repositórios são cobertos pelos dublês em memória e pelo e2e.
2. **Completar o pipeline CI/CD.** O CI já roda gitleaks, `pip-audit`, Trivy, testes, e2e e a demonstração de
   resiliência ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)). Faltam lint (ruff), SBOM,
   assinatura de imagem (cosign) e o estágio de *deploy* (CD) para homologação.
3. **OpenTelemetry** com o `traceparent` propagado no evento (trace ponta a ponta).
4. **Adaptador Azure Service Bus** para o `Publicador` e o consumidor (ADR-007).
5. **Limpeza do outbox e de `eventos_processados`**: job que remove publicados com mais de 7 dias
   (ou particionamento por data e *drop* de partição).

## Médio prazo

6. **Relatório por período** (`GET /v1/consolidado?inicio=&fim=`) com *window function* e **exportação CSV/PDF** assíncrona.
7. **Fechamento de caixa**: comando `FecharDia` que bloqueia lançamentos retroativos naquele dia e gera um snapshot
   assinado (hash encadeado) para auditoria.
8. **Snapshot do saldo acumulado** mensal para não somar o histórico inteiro a cada *cache miss*.
9. **Reprocessamento/reconstrução** do read model: comando que recalcula o `saldo_diario` a partir dos lançamentos
   (útil após bug ou mudança de regra).
10. **Ferramenta de DLQ**: inspecionar, corrigir e reenviar mensagens com trilha de auditoria.

## Longo prazo

11. **Importação automática** de lançamentos (adquirentes, Open Finance, extrato bancário) como novos produtores do
    mesmo evento, mais um contexto de **conciliação**.
12. **Kafka/Event Hubs** se surgirem muitos consumidores com necessidade de *replay* histórico.
13. **Multi-região ativo-passivo** com replicação do Postgres e failover do Front Door, se o SLO subir para 99,95%+.
14. **Detecção de anomalias** (saída de caixa fora do padrão) consumindo o mesmo evento.
