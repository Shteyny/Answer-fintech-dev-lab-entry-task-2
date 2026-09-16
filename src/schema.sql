-- ============================================================================
-- operations: одна строка = одна платёжная операция.
-- Основной источник истины по статусу и provider_payment_id.
-- ============================================================================
CREATE TABLE IF NOT EXISTS operations (
    operation_id        TEXT PRIMARY KEY,
    amount              NUMERIC(19,2) NOT NULL CHECK (amount > 0),
    currency            TEXT NOT NULL CHECK (currency = 'RUB'),
    description         TEXT,

    status              TEXT NOT NULL
                        CHECK (status IN ('CREATED','PROCESSING','COMPLETED','REJECTED')),

    provider_payment_id TEXT,

    -- Управление фоновыми повторами:
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Воркер ищет только PROCESSING с наступившим next_attempt_at.
-- Partial index делает этот запрос дешёвым независимо от размера таблицы.
CREATE INDEX IF NOT EXISTS idx_ops_due_processing
    ON operations (next_attempt_at)
    WHERE status = 'PROCESSING';


-- ============================================================================
-- events: журнал переходов состояний. event_id монотонен в пределах операции.
-- ============================================================================
CREATE TABLE IF NOT EXISTS events (
    operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
    event_id     INTEGER NOT NULL,
    type         TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    message      TEXT,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, event_id)
);


-- ============================================================================
-- receipts_seen: защита от повторных/поздних квитанций.
-- Ключ = (operation_id, provider_payment_id, result), чтобы одна и та же
-- квитанция не обрабатывалась дважды.
-- ============================================================================
CREATE TABLE IF NOT EXISTS receipts_seen (
    operation_id        TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
    provider_payment_id TEXT NOT NULL,
    result              TEXT NOT NULL CHECK (result IN ('COMPLETED','REJECTED')),
    ignored             BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, provider_payment_id, result)
);