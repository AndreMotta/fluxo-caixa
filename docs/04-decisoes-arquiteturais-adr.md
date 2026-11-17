# 4. Registro de decisões arquiteturais (ADRs)

Formato: contexto → decisão → alternativas → consequências. Todas com status **Aceita** nesta versão.

---

<a id="adr-001"></a>
## ADR-001: Microsserviços por bounded context (2 serviços), não monolito nem serverless

**Contexto.** Os dois serviços têm perfis opostos. Lançamentos é escrita crítica e não pode parar.
Consolidado é leitura com pico de 50 req/s e tolera defasagem. O requisito explícito é que a queda
do Consolidado não afete Lançamentos.

**Decisão.** Dois serviços independentes, cada um com seu banco, deploy e escala próprios.

| Alternativa | Por que não (agora) |
|---|---|
| Monolito modular | Mais simples, mas compartilha processo, pool de conexões e deploy: um pico ou vazamento de memória na consulta derruba o registro. Seria minha escolha se não houvesse o requisito de isolamento de falha |
| Serverless (Functions/Lambda) | Atende bem, mas cold start atrapalha a latência no pico, o outbox fica mais difícil de fazer bem e o custo é menos previsível. Bom candidato para o **consumidor** numa evolução |
| SOA com ESB | Pesado demais para dois contextos |
| Mais microsserviços (ex.: relay e consumidor separados) | Desnecessário agora. Relay e consumidor rodam no processo do serviço e podem ser separados por configuração (`*_ENABLED`) quando a escala pedir |

**Consequências.** (+) Isolamento de falha, escala independente, deploy independente.
(−) Consistência eventual, mais peças para operar. Mitigado com observabilidade da defasagem e do backlog.

---

<a id="adr-002"></a>
## ADR-002: Integração assíncrona por eventos com Transactional Outbox

**Contexto.** Lançamentos precisa avisar o Consolidado sem depender dele estar no ar e sem perder
nem duplicar lançamentos.

**Decisão.** Lançamento e evento são gravados na **mesma transação** do Postgres (tabela `outbox`).
Um relay publica no broker com *publisher confirms* e `mandatory=true`, e só marca como publicado após o ack.
O consumidor é **idempotente** (`eventos_processados` + soma atômica na mesma transação).

| Alternativa | Problema |
|---|---|
| Chamada HTTP síncrona Lançamentos → Consolidado | Acopla disponibilidade (fere o RNF-01) |
| Publicar no broker direto do request (dual write) | Se o broker falha depois do commit, o evento se perde; se falha antes, o lançamento falha por causa do broker |
| CDC (Debezium lendo o WAL) | Excelente, mas adiciona Kafka Connect e mais operação. É o caminho da **arquitetura de transição** para o legado (ver doc 05) |
| Consolidado lê o banco de Lançamentos | Acoplamento de dados; quebra o database-per-service |

**Consequências.** Entrega *at-least-once* com efeito *exactly-once*. A defasagem típica fica abaixo de 1 s
(intervalo do relay de 0,5 s). O backlog do outbox vira métrica e alerta.

---

<a id="adr-003"></a>
## ADR-003: Consolidado como read model pré-calculado (CQRS) + cache-aside

**Contexto.** 50 req/s no pico com ≤ 5% de perda. Calcular o saldo somando lançamentos a cada consulta
cresce com o volume e disputa o banco transacional.

**Decisão.** Manter `saldo_diario` por (comerciante, dia) atualizado por eventos com `UPSERT` atômico.
A consulta é um único `SELECT` agregado (dia + acumulado). O resultado vai para o Redis com TTL de 5 s.
O cache é *fail-open*: se cair, a consulta vai ao banco.

**Alternativas.** Materialized view com refresh (defasagem maior e refresh caro); somar on-the-fly
(não escala); cache no gateway (risco de vazar resposta entre comerciantes, então só `Cache-Control: private`).

**Consequências.** Consulta O(1) em relação ao número de lançamentos. Defasagem máxima ≈ defasagem do
evento + TTL. O acumulado soma até ~365 linhas/ano por comerciante; evolução: snapshot mensal do acumulado.

---

<a id="adr-004"></a>
## ADR-004: Stack: Python 3.12 + FastAPI, PostgreSQL 16, RabbitMQ 3.13, Redis 7.2

| Escolha | Justificativa | Alternativas consideradas |
|---|---|---|
| **Python 3.12 + FastAPI** | Linguagem que domino em produção; async nativo para I/O; validação forte com Pydantic; OpenAPI automático | Java/Spring (mais verboso), Go (ótimo desempenho, mas para o volume do desafio o ganho não compensa), .NET |
| **PostgreSQL 16** | ACID, `NUMERIC` exato para dinheiro, `ON CONFLICT`, `SKIP LOCKED`, JSONB, triggers; gerenciado em todas as nuvens | MySQL (sem `SKIP LOCKED` tão maduro em versões antigas), NoSQL (perde transação lançamento + outbox) |
| **RabbitMQ (fila quorum)** | Roteamento por tópico, DLQ, `x-delivery-limit`, publisher confirms, leve para rodar local | Kafka (melhor para replay e alto volume, mas é mais pesado do que o necessário aqui; vira opção se surgirem vários consumidores com replay); Redis Streams |
| **Redis 7.2** | Cache simples e rápido. **Licença:** 7.2 é BSD; a partir do 7.4 a licença mudou (RSAL/SSPL, depois AGPL no 8). Em produção uso o serviço gerenciado da nuvem ou **Valkey** (BSD) | Memcached, cache em memória por réplica (incoerente entre réplicas) |
| **psycopg 3 (SQL explícito)** | Controle total das queries críticas (`ON CONFLICT`, `SKIP LOCKED`), sem ORM no caminho quente | SQLAlchemy (bom, mas esconde justamente o que importa aqui) |
| **NGINX** como gateway local | Rate limit, roteamento e retry com config pequena; em nuvem é substituído por APIM | Kong, Traefik |

**Licenças:** toda a stack é open source (MIT, BSD, PostgreSQL License, MPL 2.0). Não há licença comercial
obrigatória. Custos só de infraestrutura (doc 08).

---

<a id="adr-005"></a>
## ADR-005: Idempotência de ponta a ponta

**Decisão.** (1) `Idempotency-Key` obrigatório no POST, único por comerciante, com hash do conteúdo para
detectar reuso indevido (409). (2) `event_id` único no outbox e usado como `message_id`.
(3) `eventos_processados` no consumidor, na mesma transação da soma.

**Por quê.** Rede móvel de PDV reenvia requisições, e o broker reentrega mensagens. Sem isso o saldo
fica errado, que é o pior defeito possível neste domínio.

---

<a id="adr-006"></a>
## ADR-006: Segurança de consumo: OAuth2/JWT com escopos e tenant no token

**Decisão.** Os serviços validam o JWT localmente (assinatura, `exp`, `iss`, `aud`, algoritmo fixo),
exigem escopo por operação (`lancamentos:write`, `lancamentos:read`, `consolidado:read`) e obtêm o
`comerciante_id` **somente** da claim do token. O gateway também valida (defesa em profundidade).
**Só algoritmo assimétrico:** os serviços recebem apenas a **chave pública** do IdP, então conferem tokens
mas não conseguem emitir. HS256 e `alg=none` são recusados (inclusive a confusão de algoritmo, em que a chave
pública é usada como segredo HMAC), e o serviço nem sobe com algoritmo simétrico configurado.
Local: EdDSA (Ed25519), com um IdP de desenvolvimento em container sem rede (`idp-local`), único que recebe
a chave privada. Produção: RS256/JWKS do IdP corporativo. Ver doc 06.

*Versão anterior:* HS256 com um segredo compartilhado pelos dois serviços. Quem comprometesse qualquer um deles
poderia emitir token para qualquer comerciante e escopo. Foi substituído pelo modelo acima.

---

<a id="adr-007"></a>
## ADR-007: Nuvem alvo Azure, com mensageria atrás de uma interface

**Contexto.** Localmente uso RabbitMQ. Azure não tem RabbitMQ gerenciado; AWS tem (Amazon MQ).

**Decisão.** Alvo em **Azure** (Container Apps, PostgreSQL Flexible, Service Bus Premium, APIM, Front Door,
Key Vault, Azure Monitor), com o broker atrás das interfaces `Publicador` e do tratador de mensagens.
Um adaptador Service Bus substitui o RabbitMQ sem tocar no domínio.

**Alternativa equivalente.** AWS com Amazon MQ for RabbitMQ, ECS Fargate, RDS e ElastiCache: zero mudança
de código. A escolha final depende do padrão de nuvem da organização (governança, contratos, time).

---

<a id="adr-008"></a>
## ADR-008: Consistência eventual explícita para o comerciante

**Decisão.** A resposta do consolidado traz `atualizado_em` (último evento aplicado). O cliente sabe
de quando é o número. A defasagem é medida (`consolidado_defasagem_segundos`) e tem alerta (p95 > 5 s).

---

<a id="adr-009"></a>
## ADR-009: Integridade do livro-razão e da mensageria por privilégio mínimo + assinatura de eventos

**Contexto.** Em uma revisão de segurança surgiram dois caminhos de adulteração do saldo que
autenticação na API não cobre: (1) a credencial do banco da aplicação era dona das tabelas, então
podia desligar a trigger de imutabilidade e apagar lançamentos; (2) o Consolidado aceitava qualquer
mensagem da fila, e quem tivesse a credencial do broker podia publicar um crédito falso.

**Decisão.**
1. **Banco:** dois usuários por serviço. `*_migracao` é dono do schema e só roda no job de migração.
   `*_app` é usado pelo serviço, com `SELECT, INSERT` em `lancamentos` (sem UPDATE/DELETE) e
   `UPDATE` só na coluna `outbox.publicado_em`.
2. **Broker:** a topologia é criada pela infraestrutura. `fluxo_lancamentos` só publica no exchange;
   `fluxo_consolidado` só consome a própria fila. Os serviços declaram passivamente (verificam, não criam).
3. **Eventos assinados com Ed25519 (assimétrica):** Lançamentos assina o corpo exato de cada evento com a
   **chave privada**, que só ele recebe (`x-assinatura: ed25519=<base64>`). O Consolidado recebe **apenas a
   chave pública**: confere a assinatura **antes** de qualquer processamento, mas não consegue produzir uma.
   Inválido vai para a DLQ e gera alerta de segurança. O consumidor aceita lista de chaves públicas
   ("atual,anterior") para rotação sem parada.
4. **Auditoria:** `criado_por` (= `sub`) persistido em cada lançamento e log de auditoria estruturado por escrita.

**Alternativas.**

| Alternativa | Por que não |
|---|---|
| HMAC-SHA256 com chave compartilhada (versão anterior desta solução) | A mesma chave assina e confere, então ela precisa existir nos dois serviços. Quem comprometesse o **Consolidado** (ou só lesse a configuração dele) poderia forjar créditos aceitos. Era um risco aceito; foi eliminado |
| mTLS no broker | Autentica o canal, não a mensagem: não protege contra quem tem a credencial do broker. Complementar no alvo em nuvem |
| Reconciliação periódica Consolidado × Lançamentos | Detecta divergência depois do fato. Complementar (ver evoluções) |

**Consequências.** Um serviço comprometido só consegue fazer o que já faz no fluxo normal: o Consolidado não
guarda nenhum segredo capaz de assinar. A assinatura é feita no momento da publicação (relay), então
eventos parados no outbox saem assinados com a chave vigente. Custo: mais dois jobs no deploy (migração e
topologia), um par de chaves para gerir no Key Vault e verificação Ed25519 por mensagem (dezenas de
microssegundos, irrelevante para o volume).
