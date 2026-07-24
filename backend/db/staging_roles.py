"""Enforce and verify least-privilege control-plane database roles."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from backend.config import settings

logger = logging.getLogger(__name__)

MIGRATOR_ROLE = "dbtune_migrator"
RUNTIME_ROLE = "dbtune_runtime"
BACKUP_ROLE = "dbtune_backup"
EXPECTED_ROLES = (MIGRATOR_ROLE, RUNTIME_ROLE, BACKUP_ROLE)

ROLE_GRANTS_SQL = f"""
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE};
GRANT USAGE ON SCHEMA public TO {BACKUP_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO {RUNTIME_ROLE};
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public
  TO {RUNTIME_ROLE};
REVOKE ALL ON TABLE schema_migrations FROM {RUNTIME_ROLE};
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_log FROM {RUNTIME_ROLE};
ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATOR_ROLE} IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE};
ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATOR_ROLE} IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {RUNTIME_ROLE};
"""


def _role_flags(row: asyncpg.Record) -> dict[str, bool]:
    return {
        "superuser": bool(row["rolsuper"]),
        "create_role": bool(row["rolcreaterole"]),
        "create_db": bool(row["rolcreatedb"]),
        "replication": bool(row["rolreplication"]),
        "bypass_rls": bool(row["rolbypassrls"]),
        "login": bool(row["rolcanlogin"]),
    }


async def enforce_and_verify() -> dict[str, Any]:
    conn = await asyncpg.connect(settings.database_url)
    try:
        current_user = await conn.fetchval("SELECT current_user")
        if current_user != MIGRATOR_ROLE:
            raise RuntimeError(
                f"staging role grants require {MIGRATOR_ROLE}, found {current_user}"
            )
        await conn.execute(ROLE_GRANTS_SQL)

        rows = await conn.fetch(
            """
            SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication,
                   rolbypassrls, rolcanlogin
            FROM pg_roles
            WHERE rolname = ANY($1::text[])
            ORDER BY rolname
            """,
            list(EXPECTED_ROLES),
        )
        if {row["rolname"] for row in rows} != set(EXPECTED_ROLES):
            raise RuntimeError("one or more staging database roles are missing")
        roles = {row["rolname"]: _role_flags(row) for row in rows}
        for role_name, flags in roles.items():
            unsafe = [
                name
                for name in (
                    "superuser",
                    "create_role",
                    "create_db",
                    "replication",
                    "bypass_rls",
                )
                if flags[name]
            ]
            if unsafe or not flags["login"]:
                raise RuntimeError(
                    f"unsafe flags for {role_name}: {unsafe or ['login=false']}"
                )

        membership_rows = await conn.fetch(
            """
            SELECT member.rolname AS member, granted.rolname AS granted
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted ON granted.oid = membership.roleid
            JOIN pg_roles AS member ON member.oid = membership.member
            WHERE member.rolname = ANY($1::text[])
            ORDER BY member.rolname, granted.rolname
            """,
            list(EXPECTED_ROLES),
        )
        memberships = {
            (row["member"], row["granted"]) for row in membership_rows
        }
        expected_memberships = {(BACKUP_ROLE, "pg_read_all_data")}
        if memberships != expected_memberships:
            raise RuntimeError(
                "unexpected staging role memberships: "
                f"{sorted(memberships - expected_memberships)}"
            )

        database_owner = await conn.fetchval(
            """
            SELECT owner.rolname
            FROM pg_database AS database
            JOIN pg_roles AS owner ON owner.oid = database.datdba
            WHERE database.datname = current_database()
            """
        )
        schema_owner = await conn.fetchval(
            """
            SELECT owner.rolname
            FROM pg_namespace AS namespace
            JOIN pg_roles AS owner ON owner.oid = namespace.nspowner
            WHERE namespace.nspname = 'public'
            """
        )
        if database_owner != MIGRATOR_ROLE or schema_owner != MIGRATOR_ROLE:
            raise RuntimeError(
                "database and public schema must be owned by dbtune_migrator"
            )

        wrong_owners = await conn.fetch(
            """
            SELECT c.relname, owner.rolname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            JOIN pg_roles AS owner ON owner.oid = c.relowner
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
              AND owner.rolname <> $1
            ORDER BY c.relname
            """,
            MIGRATOR_ROLE,
        )
        if wrong_owners:
            raise RuntimeError(f"public relations have unsafe owners: {dict(wrong_owners[0])}")

        runtime_owned = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            JOIN pg_roles AS owner ON owner.oid = c.relowner
            WHERE n.nspname = 'public' AND owner.rolname = $1
            """,
            RUNTIME_ROLE,
        )
        if runtime_owned:
            raise RuntimeError("dbtune_runtime must not own database objects")

        runtime_can_create = await conn.fetchval(
            "SELECT has_schema_privilege($1, 'public', 'CREATE')",
            RUNTIME_ROLE,
        )
        runtime_can_migrate = await conn.fetchval(
            "SELECT has_table_privilege($1, 'schema_migrations', 'INSERT,UPDATE,DELETE')",
            RUNTIME_ROLE,
        )
        runtime_can_mutate_audit = await conn.fetchval(
            "SELECT has_table_privilege($1, 'audit_log', 'UPDATE,DELETE,TRUNCATE')",
            RUNTIME_ROLE,
        )
        backup_can_write = await conn.fetchval(
            "SELECT has_table_privilege($1, 'hosts', 'INSERT,UPDATE,DELETE,TRUNCATE')",
            BACKUP_ROLE,
        )
        if (
            runtime_can_create
            or runtime_can_migrate
            or runtime_can_mutate_audit
            or backup_can_write
        ):
            raise RuntimeError("staging role privilege boundary verification failed")

        application_tables = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename NOT IN ('schema_migrations', 'audit_log')
            """
        )
        runtime_writable_tables = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename NOT IN ('schema_migrations', 'audit_log')
              AND has_table_privilege(
                    $1,
                    format('%I.%I', schemaname, tablename),
                    'SELECT,INSERT,UPDATE,DELETE'
                  )
            """,
            RUNTIME_ROLE,
        )
        if runtime_writable_tables != application_tables:
            raise RuntimeError(
                "dbtune_runtime is missing DML privileges on application tables"
            )

        backup_can_read = await conn.fetchval(
            "SELECT pg_has_role($1, 'pg_read_all_data', 'MEMBER')",
            BACKUP_ROLE,
        )
        if not backup_can_read:
            raise RuntimeError("dbtune_backup must inherit pg_read_all_data")

        return {
            "passed": True,
            "current_user": current_user,
            "database_owner": database_owner,
            "schema_owner": schema_owner,
            "roles": roles,
            "memberships": [
                {"member": member, "granted": granted}
                for member, granted in sorted(memberships)
            ],
            "application_tables": application_tables,
            "runtime_writable_tables": runtime_writable_tables,
            "runtime_can_create": bool(runtime_can_create),
            "runtime_can_migrate": bool(runtime_can_migrate),
            "runtime_can_mutate_audit": bool(runtime_can_mutate_audit),
            "backup_can_write": bool(backup_can_write),
            "backup_can_read_all_data": bool(backup_can_read),
        }
    finally:
        await conn.close()


def main() -> None:
    result = asyncio.run(enforce_and_verify())
    logger.info("Staging role verification passed")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
