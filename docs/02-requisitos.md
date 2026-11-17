# 2. Refinamento de requisitos

## 2.1 Requisitos funcionais

| ID | Requisito | Critérios de aceite | Status |
|---|---|---|---|
| RF-01 | Registrar lançamento de **crédito** ou **débito** | Valor > 0, até 2 casas; tipo obrigatório; data padrão = hoje (America/Sao_Paulo); data futura recusada (422) | ✅ implementado |
| RF-02 | Registro **idempotente** | Header `Idempotency-Key` obrigatório. Reenvio com mesma chave e mesmo conteúdo devolve o original (200 + `Idempotent-Replayed`), inclusive se o reenvio sem `data_competencia` chegar depois da meia-noite. Mesma chave com outro conteúdo dá 409 | ✅ |
| RF-03 | **Estornar** lançamento | Gera lançamento inverso na data de hoje; um estorno por lançamento (409); estorno de estorno proibido (422) | ✅ |
| RF-04 | Consultar lançamentos de um dia | Paginado (`limite` ≤ 500), só do próprio comerciante | ✅ |
| RF-05 | Consultar **consolidado diário** | Totais de créditos e débitos, saldo do dia, saldo acumulado, quantidade e `atualizado_em` | ✅ |
| RF-06 | Dia sem movimento | Retorna zeros e o saldo acumulado anterior (não 404) | ✅ |
| RF-07 | Lançamento retroativo | Permitido (data passada) e reflete no consolidado daquele dia e no acumulado seguinte | ✅ |
| RF-08 | Isolamento entre comerciantes | Um comerciante nunca lê nem grava dados de outro | ✅ |
| RF-09 | Relatório por período (ex.: mês) e exportação CSV/PDF | n/a | 🔜 evolução |
| RF-10 | Fechamento de caixa (bloquear lançamentos em dia fechado) | n/a | 🔜 evolução |

### Decisões de negócio tomadas (premissas que eu validaria com o PO)

1. **Lançamento é imutável.** Correção só por estorno. Preserva a trilha de auditoria e simplifica a consolidação (só soma).
2. **Estorno entra na data de hoje**, não na data original: o consolidado de dias já reportados não muda por causa de uma correção.
3. **Saldo acumulado** é entregue além do saldo do dia, porque "saldo diário consolidado" costuma significar saldo de fechamento.
4. **Multi-comerciante desde o início** (`comerciante_id` no token). Custa uma coluna e deixa a solução reutilizável como produto.
5. **Moeda única (BRL)** e valores como string decimal no JSON, para não haver erro de ponto flutuante.

## 2.2 Requisitos não funcionais (com métricas)

| ID | Atributo | Requisito / meta | Como é atendido | Como é verificado |
|---|---|---|---|---|
| RNF-01 | **Disponibilidade e isolamento de falha** | Lançamentos **não fica indisponível** se o Consolidado (ou o broker) cair | Sem chamada síncrona entre serviços; Transactional Outbox; readiness depende só do próprio banco | `test_outbox_resiliencia.py` + `make resiliencia` |
| RNF-02 | **Vazão no pico** | Consolidado com **50 req/s** e **≤ 5% de perda** | Read model pré-calculado (O(1) por consulta), cache Redis, réplicas horizontais, retry de GET no gateway, rate limit com folga de 2x | `make carga` (k6: thresholds `http_req_failed<5%` e meta interna `<0,1%`) |
| RNF-03 | Latência | Consolidado p95 < 200 ms e p99 < 500 ms; Lançamentos p95 < 300 ms | Índices, pool de conexões, cache | k6 + histograma Prometheus |
| RNF-04 | Consistência | Lançamento: forte (ACID). Consolidado: **eventual**, defasagem p95 < 5 s | Outbox + consumidor idempotente | Métrica `consolidado_defasagem_segundos` |
| RNF-05 | Durabilidade | Zero perda de lançamento aceito (RPO = 0 no registro) | Commit no Postgres antes do 201; eventos no outbox até confirmação do broker; fila quorum | Testes de outbox; DR na nuvem com HA zonal |
| RNF-06 | Exatidão | Nenhum lançamento contado duas vezes | `Idempotency-Key` + dedupe por `event_id` na mesma transação da soma | Testes de idempotência |
| RNF-07 | Disponibilidade (SLO) | Lançamentos 99,9%/mês; Consolidado 99,5%/mês | Réplicas multi-zona, health checks, failover do banco | Alertas de SLO (`infra/prometheus/alertas.yml`) |
| RNF-08 | Recuperação | RTO ≤ 15 min; RPO ≤ 5 min (banco); consolidado reconstruível a partir dos eventos/lançamentos | PITR do Postgres; replay | Runbook (evolução) |
| RNF-09 | Segurança | OAuth2/JWT, escopos por operação, isolamento por comerciante, TLS, segredos fora do código | Ver `06-seguranca.md` | Testes 401/403/IDOR |
| RNF-10 | Observabilidade | Métricas RED, logs JSON com `request_id`, alertas por SLO | Prometheus + logs estruturados | `/metrics`, regras de alerta |
| RNF-11 | Escalabilidade | Escala horizontal sem estado na aplicação | Serviços stateless; `SKIP LOCKED` no relay; consumidores concorrentes | Réplicas no compose |
| RNF-12 | Manutenibilidade | Contexto isolado, contrato versionado, testes automatizados | Database-per-service; evento `v1`; pirâmide de testes | CI (`docker build --target test`) |

### Leitura do requisito "50 req/s com no máximo 5% de perda"

- 50 req/s é **pouco** para uma consulta O(1) em cima de um read model. Uma réplica de FastAPI atende bem
  mais do que isso. O risco real é **indisponibilidade parcial** (réplica reiniciando, deploy, banco em failover),
  não CPU.
- Por isso a perda é tratada com **redundância e retry**: no mínimo 2 réplicas, retry idempotente no gateway
  (`proxy_next_upstream`), cache que absorve a leitura e degrada sem cache se o Redis cair.
- A meta interna é **≤ 0,1%**, bem abaixo dos 5% tolerados. O teste de carga também roda **100 req/s (2x)**.
- **Dimensionamento:** 50 req/s × 86.400 s = 4,3 milhões de consultas por dia de pico. Cada consulta é
  1 GET no Redis (hit) ou 1 SELECT em até ~365 linhas por comerciante/ano (miss).

## 2.3 Fora de escopo (conscientemente)

- Interface gráfica (o consumo é via API; o front seria outro contexto).
- Integração com adquirentes/bancos para importar lançamentos automaticamente (ver evoluções).
- Múltiplas moedas e regras fiscais.

## 2.4 Perguntas que eu levaria ao negócio

1. Existe fechamento de caixa? Depois de fechado, o dia pode receber lançamento retroativo?
2. O comerciante tem várias lojas/caixas? O consolidado é por loja, por CNPJ ou ambos?
3. Qual a defasagem aceitável do consolidado? (assumido: segundos)
4. Por quanto tempo os lançamentos precisam ser guardados? (fiscal: tipicamente 5 anos)
5. Quem consome o consolidado além do comerciante (contador, banco, BI)?
