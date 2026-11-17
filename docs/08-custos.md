# 8. Estimativa de custos de infraestrutura e licenças

> **Importante:** valores de referência em **USD/mês**, preço de lista aproximado, região Brazil South,
> sem impostos e sem descontos contratuais. São **ordem de grandeza** para apoiar a decisão.
> Antes de qualquer compromisso, validar na Azure Pricing Calculator com o volume real.

## 8.1 Licenças

| Item | Licença | Custo |
|---|---|---|
| Python, FastAPI, psycopg, aio-pika | PSF / MIT / LGPL-3.0 / Apache-2.0 | 0 |
| PostgreSQL | PostgreSQL License | 0 (paga-se o serviço gerenciado) |
| RabbitMQ | MPL 2.0 | 0 |
| Redis 7.2 / Valkey | BSD | 0 |
| NGINX, Prometheus, k6 | BSD-2 / Apache 2.0 / AGPL (só ferramenta de teste) | 0 |

**Não há licença comercial obrigatória.** O custo é todo de infraestrutura gerenciada e operação.

## 8.2 Premissas de dimensionamento

- Pico de 50 req/s no consolidado (≈ 4,3 mi de consultas/dia de pico) e até ~20 lançamentos/s.
- 1 ambiente de produção. Homologação a ~30% do custo de produção (instâncias menores, sem HA, desliga fora do horário).
- Retenção de logs de 30 dias quentes e ~20 GB/mês de ingestão.

## 8.3 Cenário Essencial (produção enxuta, HA onde importa)

| Componente | Configuração | USD/mês (aprox.) |
|---|---|---|
| Azure Front Door Standard | TLS, roteamento, regras WAF customizadas | 45 |
| API Management Basic v2 | validate-jwt, quotas | 150 |
| Container Apps (consumo) | 5 réplicas fixas de 0,5 vCPU/1 GiB (2 lançamentos, 2 consolidado, 1 consumidor) + picos | 250 |
| PostgreSQL Flexible: Lançamentos | General Purpose 2 vCores, **HA zonal**, 128 GB | 390 |
| PostgreSQL Flexible: Consolidado | Burstable B2ms, sem HA (reconstruível a partir dos eventos) | 90 |
| Azure Cache for Redis | Standard C1 (replicado) | 115 |
| Service Bus Standard | tópico + assinatura + DLQ | 15 |
| Observabilidade | Log Analytics/App Insights (~20 GB) + Managed Prometheus | 80 |
| Container Registry, Key Vault, backup, egress | n/a | 45 |
| **Total produção** | n/a | **≈ 1.200** |
| Homologação | ~30% | ≈ 350 |

## 8.4 Cenário Corporativo (rede privada ponta a ponta, requisitos de compliance)

| Mudança em relação ao Essencial | USD/mês adicional (aprox.) |
|---|---|
| Front Door **Premium** (WAF com regras gerenciadas OWASP + bot, Private Link) | +285 |
| APIM **Standard v2** (VNet, mais capacidade) | +550 |
| Service Bus **Premium** (VNet/Private Endpoint, isolamento) | +665 |
| Redis Premium P1 (VNet, persistência) | +300 |
| PostgreSQL do Consolidado com HA | +300 |
| Mais réplicas mínimas + observabilidade ampliada | +250 |
| **Total produção** | **≈ 3.500 a 4.000** |

## 8.5 Alavancas de otimização

- **Instâncias reservadas** de 1 a 3 anos no PostgreSQL e no Redis: redução típica de 30% a 55% nesses itens.
- **Escala a zero** em homologação e no consumidor fora do horário comercial (KEDA).
- O cache reduz a carga no banco do consolidado: dá para manter a instância pequena mesmo no pico.
- Começar no **Essencial** e evoluir para o **Corporativo** quando houver exigência regulatória ou de cliente. A arquitetura é a mesma; muda o SKU.
- Alternativa AWS (Amazon MQ, ECS Fargate, RDS Multi-AZ, ElastiCache) fica na mesma ordem de grandeza.
