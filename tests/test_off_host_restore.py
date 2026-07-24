"""Tests for independent off-host restore verification."""

from pathlib import Path

import pytest

from scripts.verify_off_host_restore import (
    build_evidence,
    client_major,
    expected_checksum,
    parse_restore_target,
    sha256_file,
)


def test_restore_target_requires_dedicated_tls_database(monkeypatch):
    target = parse_restore_target(
        "postgresql://validator:p%40ss@restore.example:6543/"
        "dbtune_restore_release42?sslmode=verify-full&sslrootcert=/ca.pem"
    )

    assert target.host == "restore.example"
    assert target.port == 6543
    assert target.user == "validator"
    assert target.password == "p@ss"
    assert target.database == "dbtune_restore_release42"
    assert target.sslmode == "verify-full"
    assert target.sslrootcert == "/ca.pem"

    monkeypatch.setenv("PATH", "/usr/bin")
    client_environment = target.client_environment()
    assert client_environment["PGPASSWORD"] == "p@ss"
    assert client_environment["PGSSLMODE"] == "verify-full"
    assert all("postgresql://" not in value for value in client_environment.values())

    with pytest.raises(ValueError, match="dbtune_restore_"):
        parse_restore_target(
            "postgresql://validator:secret@restore.example/production"
            "?sslmode=verify-full"
        )
    with pytest.raises(ValueError, match="sslmode"):
        parse_restore_target(
            "postgresql://validator:secret@restore.example/"
            "dbtune_restore_release42?sslmode=disable"
        )

    class VersionResult:
        returncode = 0
        stdout = "pg_restore (PostgreSQL) 16.14\n"
        stderr = ""

    monkeypatch.setattr(
        "scripts.verify_off_host_restore.subprocess.run",
        lambda *args, **kwargs: VersionResult(),
    )
    assert client_major("/opt/postgresql16/bin/pg_restore") == 16


def test_dump_checksum_and_evidence_are_structured(tmp_path: Path):
    dump = tmp_path / "control.dump"
    dump.write_bytes(b"database backup")
    checksum = sha256_file(dump)
    sidecar = tmp_path / "control.dump.sha256"
    sidecar.write_text(f"{checksum}  control.dump\n", encoding="utf-8")

    assert expected_checksum(sidecar) == checksum

    evidence = build_evidence(
        release_sha="a" * 40,
        verified_by="database-operator",
        restore_test_id="restore-42",
        source_host="staging-primary.example",
        restore_host="restore-validator.example",
        dump_checksum=checksum,
        dump_size_bytes=dump.stat().st_size,
        migrations=19,
        tables=29,
        postgres_major=16,
    )
    gate = evidence["gates"]["independent_off_host_restore"]
    assert gate["verified"] is True
    assert gate["evidence"]["source_backup_sha256"] == checksum
    assert gate["measurements"] == {
        "dump_size_bytes": 15,
        "postgres_major": 16,
        "schema_migrations": 19,
        "public_tables": 29,
    }
