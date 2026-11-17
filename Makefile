.PHONY: env chaves up aguardar down logs test e2e token carga resiliencia observabilidade seguranca segredos auditoria-deps auditoria-imagens

# Ferramentas fixadas por digest (cadeia de suprimentos). Atualizadas pelo Dependabot/manualmente.
PYTHON   := python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
K6       := grafana/k6:0.54.0@sha256:1f40432b1cbe7234e977f96c362c9bc550a2d2b583d014dd8669fe40d3e9e755
TRIVY    := aquasec/trivy:0.57.1@sha256:5c59e08f980b5d4d503329773480fcea2c9bdad7e381d846fbf9f2ecb8050f6b
GITLEAKS := zricethezav/gitleaks:v8.21.2@sha256:0e99e8821643ea5b235718642b93bb32486af9c8162c8b8731f7cbdc951a7f46

# Gera os pares Ed25519 (eventos e IdP local) com o código do próprio serviço, num container sem rede.
GERAR_CHAVES := docker build -q --target runtime -t fluxo-lancamentos:chaves services/lancamentos >/dev/null \
	&& docker run --rm --network none fluxo-lancamentos:chaves python -m app.gerar_chaves

# Emite um JWT pelo IdP local (container sem rede; único que recebe a chave privada dos tokens).
# O script filtra a saída do docker compose e devolve só o token.
EMITIR_TOKEN := bash scripts/token.sh

env:            ## cria o .env a partir do exemplo e gera os pares de chaves (eventos e IdP); troque as senhas!
	@test -f .env || { cp .env.example .env && $(GERAR_CHAVES) >> .env \
		&& echo ".env criado com pares de chaves novos (eventos e IdP local)" || { rm -f .env; exit 1; }; }

chaves:         ## imprime pares Ed25519 novos (rotação: substitua as linhas correspondentes do .env)
	@$(GERAR_CHAVES)

up: env         ## sobe o ambiente completo (gateway em http://localhost:8080)
	docker compose up -d --build

aguardar:       ## espera as réplicas de lançamentos e consolidado ficarem healthy (até ~3 min)
	@for i in $$(seq 1 60); do \
	  n=$$(docker compose ps --format '{{.Service}} {{.Health}}' lancamentos consolidado | grep -c ' healthy$$' || true); \
	  if [ "$$n" -ge 4 ]; then echo "serviços prontos"; exit 0; fi; sleep 3; \
	done; docker compose ps; exit 1

down:           ## derruba o ambiente e apaga os volumes
	docker compose down -v

logs:
	docker compose logs -f lancamentos consolidado

test:           ## testes unitários + de API dos dois serviços (dentro do Docker)
	docker build --target test -t fluxo-lancamentos:test services/lancamentos
	docker build --target test -t fluxo-consolidado:test services/consolidado

e2e:            ## teste ponta a ponta contra o ambiente em execução
	docker compose --profile e2e run --rm e2e

token:          ## emite um JWT de desenvolvimento pelo IdP local (COMERCIANTE=loja-1)
	@$(EMITIR_TOKEN) --comerciante $${COMERCIANTE:-loja-1}

carga:          ## teste de carga do pico de 50 req/s (k6 em container)
	@TOKEN=$$($(EMITIR_TOKEN) --comerciante carga); \
	docker run --rm -i --network fluxo-caixa_borda -e TOKEN=$$TOKEN -e BASE_URL=http://gateway:8080 \
		$(K6) run - < loadtest/consolidado-pico.js

resiliencia:    ## derruba consolidado+broker e prova que lançamentos seguem no ar sem perda
	bash scripts/demo-resiliencia.sh

observabilidade: ## sobe o Prometheus (http://localhost:9090) com as regras de alerta
	docker compose --profile observabilidade up -d prometheus

seguranca: segredos auditoria-deps auditoria-imagens ## todas as verificações de segurança do CI

segredos:       ## segredos versionados por engano (gitleaks)
	docker run --rm -v "$$PWD:/repo:ro" $(GITLEAKS) detect --no-git --source /repo \
		--config /repo/.gitleaks.toml --redact -v

auditoria-deps: ## CVEs conhecidas nas dependências Python (pip-audit / OSV)
	docker run --rm -v "$$PWD/services:/s:ro" $(PYTHON) sh -c "\
		pip install -q --disable-pip-version-check pip-audit==2.10.1 && \
		pip-audit -r /s/lancamentos/requirements.txt && \
		pip-audit -r /s/consolidado/requirements.txt"

auditoria-imagens: ## CVEs nas imagens dos serviços (Trivy; falha em CRITICAL com correção disponível)
	docker build -q --target runtime -t fluxo-lancamentos:scan services/lancamentos
	docker build -q --target runtime -t fluxo-consolidado:scan services/consolidado
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock $(TRIVY) image \
		--exit-code 1 --severity CRITICAL --ignore-unfixed fluxo-lancamentos:scan
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock $(TRIVY) image \
		--exit-code 1 --severity CRITICAL --ignore-unfixed fluxo-consolidado:scan
