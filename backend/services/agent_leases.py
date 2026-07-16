"""Single-writer Host Agent lease and duplicate-instance detection."""

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from uuid import UUID

from backend.config import settings
from backend.services.operational_events import OperationalEventRecorder


@dataclass(frozen=True)
class AgentLeaseState:
    host_id: UUID
    instance_id: UUID
    active_instances: int
    write_ambiguous: bool
    transitioned: Optional[str] = None


async def renew_agent_lease(
    connection,
    host_id: UUID,
    instance_id: UUID,
    *,
    details: Optional[Mapping[str, Any]] = None,
) -> AgentLeaseState:
    """Renew one instance and atomically recalculate host write ownership."""
    recorder = OperationalEventRecorder()
    async with connection.transaction():
        host = await connection.fetchrow(
            """
            SELECT id, organization_id, hostname, agent_write_ambiguous
            FROM hosts WHERE id = $1 FOR UPDATE
            """,
            host_id,
        )
        if host is None:
            raise ValueError(f"Host {host_id} does not exist")
        await connection.execute(
            """
            INSERT INTO host_agent_instances (
                host_id, instance_id, organization_id, first_seen_at,
                last_seen_at, lease_expires_at, details
            ) VALUES (
                $1, $2, $3, NOW(), NOW(),
                NOW() + ($4::text || ' seconds')::interval, $5::jsonb
            )
            ON CONFLICT (host_id, instance_id) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                last_seen_at = NOW(),
                lease_expires_at = EXCLUDED.lease_expires_at,
                details = EXCLUDED.details
            """,
            host_id,
            instance_id,
            host["organization_id"],
            str(settings.agent_lease_seconds),
            json.dumps(dict(details or {})),
        )
        active_rows = await connection.fetch(
            """
            SELECT instance_id, lease_expires_at
            FROM host_agent_instances
            WHERE host_id = $1 AND lease_expires_at > NOW()
            ORDER BY first_seen_at, instance_id
            """,
            host_id,
        )
        active_count = len(active_rows)
        ambiguous = active_count > 1
        previous = bool(host["agent_write_ambiguous"])
        holder = active_rows[0]["instance_id"] if active_count == 1 else None
        expires_at = active_rows[0]["lease_expires_at"] if active_count == 1 else None
        await connection.execute(
            """
            UPDATE hosts
            SET agent_write_ambiguous = $2,
                agent_lease_holder_id = $3,
                agent_lease_expires_at = $4,
                updated_at = NOW()
            WHERE id = $1
            """,
            host_id,
            ambiguous,
            holder,
            expires_at,
        )

        transition = None
        instance_ids = [str(row["instance_id"]) for row in active_rows]
        if ambiguous and not previous:
            transition = "duplicate_detected"
            await recorder.record(
                "AGENT_DUPLICATE_DETECTED",
                f"Multiple active Host Agents detected for {host['hostname']}; writes blocked",
                organization_id=host["organization_id"],
                host_id=host_id,
                details={"active_instance_ids": instance_ids, "active_count": active_count},
                connection=connection,
            )
        elif previous and not ambiguous:
            transition = "duplicate_resolved"
            await recorder.record(
                "AGENT_DUPLICATE_RESOLVED",
                f"Host Agent ownership for {host['hostname']} is unambiguous again",
                organization_id=host["organization_id"],
                host_id=host_id,
                details={"active_instance_ids": instance_ids, "active_count": active_count},
                connection=connection,
            )

    return AgentLeaseState(host_id, instance_id, active_count, ambiguous, transition)
