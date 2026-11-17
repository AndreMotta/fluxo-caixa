-- Contexto de Consolidado Diário: read model pré-calculado + controle de idempotência.

CREATE TABLE IF NOT EXISTS saldo_diario (
    comerciante_id VARCHAR(64)    NOT NULL,
    data           DATE           NOT NULL,
    total_creditos NUMERIC(16, 2) NOT NULL DEFAULT 0,
    total_debitos  NUMERIC(16, 2) NOT NULL DEFAULT 0,
    quantidade     INTEGER        NOT NULL DEFAULT 0,
    atualizado_em  TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (comerciante_id, data)
);

-- event_id já aplicado: garante exactly-once no efeito, sobre entrega at-least-once.
CREATE TABLE IF NOT EXISTS eventos_processados (
    event_id       UUID        PRIMARY KEY,
    lancamento_id  UUID        NOT NULL,
    comerciante_id VARCHAR(64) NOT NULL,
    processado_em  TIMESTAMPTZ NOT NULL DEFAULT now()
);
