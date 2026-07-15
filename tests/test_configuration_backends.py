"""Configuration backend routing and public-provenance safety tests."""

from uuid import uuid4

import pytest

from backend.services.configuration_backends import (
    ManagedConfFileBackend,
    ProviderConfigurationBackend,
    register_provider_adapter,
)
from backend.services.target_executor import ExecutionResult


@pytest.mark.asyncio
async def test_missing_provider_adapter_fails_closed_without_file_fallback():
    backend = ProviderConfigurationBackend(None, "aws_rds", None)

    result = await backend.dry_run(
        uuid4(), [{"setting_name": "work_mem", "proposed_value": "8MB"}]
    )

    assert result.passed is False
    assert result.snapshot == {}
    assert result.errors == ["Provider backend adapter is not configured for aws_rds"]


def test_provider_registration_rejects_self_managed_platform():
    with pytest.raises(ValueError, match="Unsupported managed platform"):
        register_provider_adapter("self_managed", object())


def test_public_execution_result_redacts_exact_file_bytes():
    private = {
        "file": {
            "existed": True,
            "bytes_b64": "c2VjcmV0",
            "checksum": "abc",
        }
    }
    result = ExecutionResult(True, ["work_mem"], {"work_mem": "8MB"}, backend_snapshot=private)

    public = result.to_dict()

    assert public["backend_snapshot"]["file"] == {
        "existed": True,
        "checksum": "abc",
    }
    assert private["file"]["bytes_b64"] == "c2VjcmV0"
    assert ManagedConfFileBackend._has_exact_file_snapshot(private) is True
    assert ManagedConfFileBackend._has_exact_file_snapshot(public["backend_snapshot"]) is False
