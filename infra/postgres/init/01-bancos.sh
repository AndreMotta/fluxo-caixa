#!/bin/sh
# Um banco por serviço (database-per-service) e DOIS usuários por banco:
#   *_migracao: dono do schema, usado só pelo job de migração (DDL);
#   *_app:      usado pelo serviço em execução, só com os grants da migração 002 (DML mínimo).
# Nenhum usuário conecta no banco do outro serviço.
# Executado uma única vez pelo entrypoint do Postgres, na criação do volume.
# ON_ERROR_STOP faz o psql abortar no primeiro erro.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE lancamentos_migracao LOGIN PASSWORD '${LANC_DB_MIGRACAO_PASSWORD}';
    CREATE ROLE lancamentos_app      LOGIN PASSWORD '${LANC_DB_PASSWORD}';
    CREATE DATABASE lancamentos OWNER lancamentos_migracao;
    REVOKE ALL ON DATABASE lancamentos FROM PUBLIC;
    GRANT CONNECT ON DATABASE lancamentos TO lancamentos_app;

    CREATE ROLE consolidado_migracao LOGIN PASSWORD '${CONS_DB_MIGRACAO_PASSWORD}';
    CREATE ROLE consolidado_app      LOGIN PASSWORD '${CONS_DB_PASSWORD}';
    CREATE DATABASE consolidado OWNER consolidado_migracao;
    REVOKE ALL ON DATABASE consolidado FROM PUBLIC;
    GRANT CONNECT ON DATABASE consolidado TO consolidado_app;
EOSQL
