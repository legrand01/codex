"""Provision a control-plane API principal and print its key exactly once."""

import argparse
import asyncio
import secrets

import asyncpg

from backend.config import settings
from backend.security import hash_token


async def create_principal(args) -> None:
    token = secrets.token_urlsafe(32)
    conn = await asyncpg.connect(settings.database_url)
    try:
        organization_id = await conn.fetchval(
            "SELECT id FROM organizations WHERE slug = $1",
            args.organization,
        )
        if organization_id is None:
            raise RuntimeError(f"Organization {args.organization!r} does not exist")
        await conn.execute(
            """
            INSERT INTO api_principals (
                organization_id, subject, display_name, role, api_key_hash
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            organization_id,
            args.subject,
            args.display_name,
            args.role,
            hash_token(token),
        )
    finally:
        await conn.close()
    print(token)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--organization", default="default")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument(
        "--role",
        required=True,
        choices=("viewer", "operator", "approver", "admin"),
    )
    asyncio.run(create_principal(parser.parse_args()))


if __name__ == "__main__":
    main()
