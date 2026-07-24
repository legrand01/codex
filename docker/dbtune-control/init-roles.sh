#!/bin/sh
set -eu

required_variables="
POSTGRES_USER
POSTGRES_PASSWORD
POSTGRES_DB
POSTGRES_MIGRATION_PASSWORD
POSTGRES_RUNTIME_PASSWORD
POSTGRES_BACKUP_PASSWORD
"
for variable in $required_variables; do
  eval "value=\${$variable:-}"
  if [ -z "$value" ]; then
    echo "$variable is required" >&2
    exit 1
  fi
done

export PGPASSWORD="$POSTGRES_PASSWORD"

psql \
  --host="${PGHOST:-postgres}" \
  --port="${PGPORT:-5432}" \
  --username="$POSTGRES_USER" \
  --dbname=postgres \
  --set=ON_ERROR_STOP=1 <<'SQL'
\getenv migration_password POSTGRES_MIGRATION_PASSWORD
\getenv runtime_password POSTGRES_RUNTIME_PASSWORD
\getenv backup_password POSTGRES_BACKUP_PASSWORD
\getenv database_name POSTGRES_DB

SELECT format(
    'CREATE ROLE dbtune_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'migration_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dbtune_migrator')
\gexec
SELECT format(
    'CREATE ROLE dbtune_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'runtime_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dbtune_runtime')
\gexec
SELECT format(
    'CREATE ROLE dbtune_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'backup_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dbtune_backup')
\gexec

ALTER ROLE dbtune_migrator
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'migration_password';
ALTER ROLE dbtune_runtime
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'runtime_password';
ALTER ROLE dbtune_backup
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'backup_password';

SELECT format('REVOKE %I FROM %I', granted.rolname, member.rolname)
FROM pg_auth_members AS membership
JOIN pg_roles AS granted ON granted.oid = membership.roleid
JOIN pg_roles AS member ON member.oid = membership.member
WHERE member.rolname IN ('dbtune_migrator', 'dbtune_runtime', 'dbtune_backup')
  AND NOT (
    member.rolname = 'dbtune_backup'
    AND granted.rolname = 'pg_read_all_data'
  )
ORDER BY member.rolname, granted.rolname
\gexec
GRANT pg_read_all_data TO dbtune_backup;

SELECT format('ALTER DATABASE %I OWNER TO dbtune_migrator', :'database_name')
\gexec
SQL

psql \
  --host="${PGHOST:-postgres}" \
  --port="${PGPORT:-5432}" \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 <<'SQL'
\getenv database_name POSTGRES_DB

SELECT format(
    'ALTER %s %I.%I OWNER TO dbtune_migrator',
    CASE c.relkind
      WHEN 'r' THEN 'TABLE'
      WHEN 'p' THEN 'TABLE'
      WHEN 'S' THEN 'SEQUENCE'
      WHEN 'v' THEN 'VIEW'
      WHEN 'm' THEN 'MATERIALIZED VIEW'
      WHEN 'f' THEN 'FOREIGN TABLE'
    END,
    n.nspname,
    c.relname
)
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
ORDER BY c.relkind, c.relname
\gexec

SELECT format(
    'ALTER %s %I.%I(%s) OWNER TO dbtune_migrator',
    CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
    n.nspname,
    p.proname,
    pg_get_function_identity_arguments(p.oid)
)
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
\gexec

ALTER SCHEMA public OWNER TO dbtune_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO dbtune_migrator, dbtune_runtime, dbtune_backup',
    :'database_name'
)
\gexec
GRANT USAGE ON SCHEMA public TO dbtune_runtime;
GRANT USAGE ON SCHEMA public TO dbtune_backup;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO dbtune_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public
  TO dbtune_runtime;
SELECT 'REVOKE ALL ON TABLE schema_migrations FROM dbtune_runtime'
WHERE to_regclass('public.schema_migrations') IS NOT NULL
\gexec
SELECT 'REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_log FROM dbtune_runtime'
WHERE to_regclass('public.audit_log') IS NOT NULL
\gexec

ALTER DEFAULT PRIVILEGES FOR ROLE dbtune_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dbtune_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE dbtune_migrator IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO dbtune_runtime;
SQL

echo "Control-plane database roles initialized with least privilege"
