-- Contexto de Lançamentos: livro-razão append-only + outbox transacional.

CREATE TABLE IF NOT EXISTS lancamentos (
    id               UUID PRIMARY KEY,
    comerciante_id   VARCHAR(64)    NOT NULL,
    tipo             VARCHAR(7)     NOT NULL CHECK (tipo IN ('CREDITO', 'DEBITO')),
    valor            NUMERIC(14, 2) NOT NULL CHECK (valor > 0),
    data_competencia DATE           NOT NULL,
    descricao        VARCHAR(140)   NOT NULL DEFAULT '',
    criado_em        TIMESTAMPTZ    NOT NULL,
    criado_por       VARCHAR(128)   NOT NULL,  -- `sub` do token (auditoria / não-repúdio)
    estorno_de       UUID           NULL REFERENCES lancamentos (id),
    idempotency_key  VARCHAR(64)    NOT NULL,
    hash_requisicao  CHAR(64)       NOT NULL,
    CONSTRAINT uq_lancamentos_idempotencia UNIQUE (comerciante_id, idempotency_key)
);

-- Um lançamento só pode ser estornado uma vez.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lancamentos_estorno_de
    ON lancamentos (estorno_de) WHERE estorno_de IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_lancamentos_comerciante_data
    ON lancamentos (comerciante_id, data_competencia, criado_em);

CREATE TABLE IF NOT EXISTS outbox (
    id           BIGSERIAL PRIMARY KEY,
    event_id     UUID         NOT NULL UNIQUE,
    aggregate_id UUID         NOT NULL,
    routing_key  VARCHAR(100) NOT NULL,
    payload      JSONB        NOT NULL,
    criado_em    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    publicado_em TIMESTAMPTZ  NULL
);

-- Índice parcial: o relay só varre o que está pendente.
CREATE INDEX IF NOT EXISTS ix_outbox_pendentes ON outbox (id) WHERE publicado_em IS NULL;

-- Imutabilidade garantida no banco, não só na aplicação.
CREATE OR REPLACE FUNCTION fn_lancamentos_imutaveis() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'lancamentos são imutáveis: use estorno';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_lancamentos_imutaveis
    BEFORE UPDATE OR DELETE ON lancamentos
    FOR EACH ROW EXECUTE FUNCTION fn_lancamentos_imutaveis();
