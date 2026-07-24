\set ON_ERROR_STOP on
\getenv target_agent_user POSTGRES_AGENT_USER
\getenv target_agent_password POSTGRES_AGENT_PASSWORD
\getenv target_workload_user POSTGRES_WORKLOAD_USER
\getenv target_workload_password POSTGRES_WORKLOAD_PASSWORD

SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'target_agent_user',
    :'target_agent_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'target_agent_user'
)
\gexec

SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'target_workload_user',
    :'target_workload_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'target_workload_user'
)
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'target_agent_user',
    :'target_agent_password'
)
\gexec
SELECT format(
    'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'target_workload_user',
    :'target_workload_password'
)
\gexec

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

GRANT CONNECT ON DATABASE :"DBNAME" TO :"target_agent_user", :"target_workload_user";
GRANT USAGE ON SCHEMA public TO :"target_agent_user", :"target_workload_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
TO :"target_workload_user";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public
TO :"target_workload_user";
