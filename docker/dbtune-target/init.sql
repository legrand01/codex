CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE accounts (
    id BIGINT PRIMARY KEY,
    balance NUMERIC(14, 2) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO accounts (id, balance)
SELECT value, 10000.00
FROM generate_series(1, 100000) AS value;

CREATE TABLE ledger (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(id),
    amount NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ledger_account_created_idx ON ledger (account_id, created_at DESC);

CREATE TABLE sales_events (
    id BIGSERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    region_id INTEGER NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

INSERT INTO sales_events (customer_id, region_id, amount, created_at)
SELECT
    1 + (value % 25000),
    1 + (value % 40),
    (10 + (value % 990))::numeric(12, 2),
    NOW() - ((value % 2592000) * INTERVAL '1 second')
FROM generate_series(1, 750000) AS value;

CREATE INDEX sales_events_created_idx ON sales_events (created_at);
ANALYZE;
