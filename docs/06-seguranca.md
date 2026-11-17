# 6. Segurança e critérios para consumo (integração) de serviços

Princípios: **zero trust** entre componentes, **least privilege**, **defesa em profundidade** e
**secure by default**.

Legenda: ✅ implementado e coberto por teste ou configuração neste repositório · 🔜 alvo em nuvem / evolução.
Nada aqui é marcado como implementado sem estar no código.

## 6.1 Modelo de ameaças (STRIDE)

| Ameaça | Exemplo no domínio | Controles | Evidência |
|---|---|---|---|
| **S**poofing | Cliente se passa por outro comerciante; token forjado | ✅ JWT com `iss`/`aud`/`exp`/`iat`/`sub` obrigatórios e algoritmo fixo; ✅ **só algoritmo assimétrico** (EdDSA local, RS256/JWKS em produção): os serviços têm apenas a chave pública do IdP, não conseguem emitir token e recusam HS256, `alg=none` e a confusão de algoritmo; ✅ chave privada do IdP local só no container `idp-local` (sem rede) | `security.py`, `test_token_com_alg_none_e_recusado`, `test_confusao_de_algoritmo_hs256_com_a_chave_publica_e_recusada`, `test_token_assinado_por_outra_chave_privada_e_recusado` |
| **S**poofing (mensageria) | Alguém com acesso ao broker publica um crédito falso | ✅ Cada evento assinado com **Ed25519** (`x-assinatura: ed25519=…`): a chave privada fica só com Lançamentos e o Consolidado tem apenas a pública, então nem ele consegue forjar um evento; o Consolidado descarta (DLQ) e alerta qualquer evento sem assinatura válida; rotação de chave sem parada | `assinatura.py`, `consumer.py`, `test_evento_sem_assinatura_valida_nao_altera_saldo`, `test_evento_assinado_por_outra_chave_privada_nao_altera_saldo` |
| **T**ampering | Alterar/apagar lançamento para esconder saída de caixa | ✅ O usuário de execução **não é dono** das tabelas e só tem `SELECT, INSERT` em `lancamentos`: não consegue fazer UPDATE/DELETE nem desligar a trigger. ✅ A trigger bloqueia UPDATE/DELETE até para o dono. Correção só por estorno | `migrations/002_privilegio_minimo.sql`, `infra/postgres/init` |
| **R**epudiation | "Não fui eu que lancei" | ✅ `criado_por` (= `sub` do token) gravado no lançamento e no estorno; ✅ log de auditoria JSON a cada escrita (ação, sujeito, comerciante, valor, `request_id`, `Idempotency-Key`); 🔜 log de auditoria roteado para armazenamento imutável (WORM) | `service.py`, `test_escrita_gera_log_de_auditoria` |
| **I**nformation disclosure | Comerciante A ler o saldo de B (IDOR) | ✅ `comerciante_id` só vem do token; toda query filtra por ele; ✅ `Cache-Control: private`; ✅ chave de cache inclui o comerciante; ✅ `criado_por` não trafega no evento (minimização) | `test_comerciante_nao_enxerga_dados_de_outro` |
| **D**enial of service | Rajada derrubando a consulta no pico | ✅ Rate limit no gateway, corpo ≤ 16 KB, timeouts, paginação com teto; 🔜 WAF + quotas por `client_id` no APIM | `nginx.conf` |
| **E**levation of privilege | Token de leitura tentando gravar; serviço comprometido tentando ir além | ✅ Escopo por operação (403); ✅ usuários de banco e de broker com o mínimo; ✅ containers non-root, `cap_drop: ALL`, `no-new-privileges`, FS somente leitura | `test_exige_escopo_de_escrita`, `docker-compose.yml` |

## 6.2 Critérios para consumir os serviços (contrato de integração)

Qualquer sistema cliente (PDV, app, backoffice, parceiro) precisa cumprir:

1. **Autenticação OAuth2/OIDC.** Usuário: *authorization code + PKCE*. Sistema/parceiro: *client credentials*
   (preferencialmente com `private_key_jwt` ou certificado, em vez de *client secret*).
2. **Token de vida curta** (≤ 15 min), audiência `fluxo-caixa`, escopos mínimos por cliente:

   | Cliente | Escopos |
   |---|---|
   | PDV | `lancamentos:write` |
   | App do comerciante | `lancamentos:write lancamentos:read consolidado:read` |
   | Contador / BI | `consolidado:read` |

3. **TLS 1.2+** obrigatório na borda (Front Door); **mTLS** opcional para parceiros B2B.
4. **`Idempotency-Key`** em toda escrita; retentativas com *backoff* exponencial e *jitter*, respeitando `429` e `Retry-After`.
5. **Quotas por cliente** (APIM): limites por `client_id`, não só por IP.
6. **Versionamento** de API (`/v1`) e de evento (`v1`), com prazo de descontinuação comunicado.
7. **Sem dados pessoais** além do necessário: a descrição é texto livre de até 140 caracteres; a orientação é não colocar
   CPF/cartão ali. 🔜 Detecção de PII no pipeline de logs.

## 6.3 Comunicação entre serviços (leste-oeste)

| Controle | Local (este repositório) | Alvo em nuvem |
|---|---|---|
| Segmentação de rede | ✅ Redes `borda` × `dados`: o gateway não alcança banco, broker nem cache; ✅ gateway e consoles publicados só em `127.0.0.1` | VNet + subnets, Private Endpoints, NSG; nada público além do Front Door |
| Banco | ✅ Banco por serviço; ✅ **dois usuários por banco**: `*_migracao` (dono, DDL, só no job de migração) e `*_app` (DML mínimo); ✅ nenhum usuário conecta no banco do outro serviço | Servidores separados; Managed Identity; migração no pipeline |
| Broker | ✅ Topologia criada pela infraestrutura (job `rabbitmq-topologia`), não pelos serviços; ✅ `fluxo_lancamentos` só **publica** no exchange; `fluxo_consolidado` só **consome** a própria fila; ✅ admin só para topologia/console | Service Bus com SAS/RBAC por entidade: *Send* no tópico, *Listen* na assinatura |
| Integridade das mensagens | ✅ Assinatura Ed25519 por evento, verificada antes de qualquer processamento; ✅ chave privada só no container de Lançamentos, chave pública só no Consolidado (compose); ✅ alerta `EventoComAssinaturaInvalida` | Chave privada no Key Vault, legível só pela identidade de Lançamentos; o Consolidado lê só a pública. Rotação: o consumidor aceita "atual,anterior" |
| Cache | ✅ Senha obrigatória, sem persistência | TLS, autenticação Entra, sem acesso público |
| Em trânsito | Rede interna do Docker (sem TLS) | TLS em todas as conexões (Postgres `sslmode=verify-full`, AMQPS, Redis TLS) |
| Em repouso | Volume local | Criptografia gerenciada (CMK no Key Vault quando exigido por compliance) |

## 6.4 Segredos

- ✅ Nenhum segredo no código; `.env` fora do Git; `.env.example` só com valores ilustrativos.
- ✅ O compose não sobe sem as chaves obrigatórias (`${VAR:?}`); os serviços recusam subir sem chave válida ou com algoritmo de token simétrico.
- ✅ **Nenhum segredo de assinatura é compartilhado entre serviços.** `make env` (ou `make chaves`) gera dois pares Ed25519:

  | Par | Chave privada (assina) | Chave pública (confere) |
  |---|---|---|
  | Eventos | só Lançamentos | só Consolidado |
  | Tokens (IdP local) | só o container `idp-local` (sem rede) e o e2e | Lançamentos e Consolidado |

  Comprometer qualquer serviço não dá ao atacante uma chave capaz de emitir token nem de forjar evento aceito por outro.
- ✅ No CI, os dois pares de chaves são gerados novos a cada execução.
- **Rotação da chave de eventos sem parada:** (1) `make chaves` gera pares novos (use as linhas `EVENTOS_*`); (2) o Consolidado passa a aceitar
  "nova,antiga" em `CONS_EVENTOS_CHAVES_PUBLICAS`; (3) Lançamentos troca a chave privada; (4) quando a fila esvaziar,
  remove-se a pública antiga. Como a assinatura acontece na publicação, eventos parados no outbox já saem com a chave nova.
- 🔜 Key Vault + Managed Identity; rotação automática; tokens emitidos pelo IdP corporativo (RS256/JWKS), que já é suportado.

## 6.5 Cadeia de suprimentos e pipeline (DevSecOps)

| Etapa | Controle |
|---|---|
| Dependências | ✅ Versões fixadas e verificadas no OSV (nov/2026); ✅ **pip-audit no CI**; ✅ Dependabot (pip, Docker, Compose, Actions) |
| Código | ✅ **gitleaks no CI** (segredos versionados); 🔜 SAST (Semgrep/SonarQube) |
| Imagens | ✅ Base e imagens de terceiros **fixadas por digest**; ✅ **Trivy no CI** (bloqueia CRITICAL com correção); ✅ usuário 10001; 🔜 SBOM (Syft) e assinatura (cosign) |
| Pipeline | ✅ Actions fixadas por **SHA**; ✅ token do workflow só leitura; ✅ `persist-credentials: false` |
| Runtime | ✅ `read_only`, `cap_drop: ALL`, `no-new-privileges`, limites de CPU/memória |
| Testes | ✅ 401/403, `alg=none`, IDOR, validação, idempotência, assinatura de evento forjada; 🔜 DAST (OWASP ZAP) |

Comando único local: `make seguranca` (gitleaks + pip-audit + Trivy).

## 6.6 Superfície exposta

- Pelo gateway passam apenas `/v1/lancamentos*` e `/v1/consolidado*`.
- ✅ Swagger/OpenAPI **desligado por padrão** (`*_DOCS_HABILITADO=false`); só o compose local liga (teste confirma 404 com o padrão).
- ✅ `/metrics` e `/health/*` não são expostos na borda (teste e2e confirma 404).
- ✅ Erros em *Problem Details* (RFC 9457) sem stack trace; o `request_id` correlaciona com o log. IDs de correlação malformados vindos de fora são descartados (evita injeção em log).

## 6.7 Detecção (o que o SOC recebe)

| Sinal | Origem | Uso |
|---|---|---|
| `evento=acesso_negado` (motivo, sujeito, rota, origem) + métrica `acessos_negados_total` | Serviços | Força bruta de token, varredura de escopos (alerta `PicoDeAcessosNegados`) |
| `evento=assinatura_invalida` + `consolidado_eventos_total{resultado="assinatura_invalida"}` | Consolidado | Adulteração na mensageria (alerta `EventoComAssinaturaInvalida`, severidade page) |
| `evento=auditoria` | Lançamentos | Trilha de quem registrou/estornou o quê |

## 6.8 Riscos residuais (conhecidos e aceitos nesta versão)

| Risco | Por que ainda existe | Tratamento |
|---|---|---|
| Sem TLS entre containers no ambiente local | Simplicidade do ambiente de avaliação; tudo em `127.0.0.1` e redes internas | TLS obrigatório no alvo em nuvem |
| Dependências sem *hash lock* (`--require-hashes`) | Gerar o lock exige resolver em Python 3.12 e não houve ambiente para isso | Gerar com `uv pip compile --generate-hashes` no CI (evolução) |
| Imagens de terceiros em versões antigas (RabbitMQ 3.13, NGINX 1.27, Postgres 16, Redis 7.2) | Fixadas por digest, mas não verificadas quanto a fim de suporte | Dependabot propõe atualizações; Trivy estendido às imagens de terceiros (evolução) |
| Rate limit por IP | Atrás de proxy/CDN, o IP visto é o do proxy | Quotas por `client_id` no APIM |

## 6.9 Conformidade (LGPD)

- Dados tratados são financeiros do comerciante (pessoa jurídica, na maioria). Se o comerciante for pessoa física, `comerciante_id` é dado pessoal: pseudonimizar nos logs.
- `criado_por` guarda o identificador opaco do IdP (`sub`), não nome nem e-mail; não é enviado ao Consolidado.
- Hospedagem em Brazil South evita transferência internacional do dado principal. Atenção a SaaS de observabilidade fora do país.
- Retenção: lançamentos por 5 anos (obrigação fiscal típica, a validar com o jurídico); logs de aplicação por 90 dias; auditoria conforme política.
