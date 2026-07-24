"""Tests for the repository-wide mypy debt baseline."""

from collections import Counter
from pathlib import Path

import pytest

from scripts.check_mypy_baseline import (
    Fingerprint,
    deserialize,
    format_changes,
    parse_errors,
    serialize,
)


def test_parse_errors_ignores_notes_and_line_numbers(tmp_path: Path) -> None:
    output = "\n".join(
        [
            f"{tmp_path}/backend/example.py:10: error: Missing return [return]",
            f"{tmp_path}/backend/example.py:99:5: error: Missing return [return]",
            "backend/other.py:4: note: A useful note",
            "Found 2 errors in 1 file",
        ]
    )

    assert parse_errors(output, tmp_path) == Counter(
        {
            Fingerprint(
                path="backend/example.py",
                code="return",
                message="Missing return",
            ): 2
        }
    )


def test_baseline_serialization_is_deterministic_and_validated() -> None:
    errors = Counter(
        {
            Fingerprint("backend/z.py", "arg-type", "Bad argument"): 2,
            Fingerprint("backend/a.py", "type-arg", "Missing parameter"): 1,
        }
    )

    payload = serialize(errors)

    assert payload["error_count"] == 3
    assert deserialize(payload) == errors
    assert payload["fingerprints"][0]["path"] == "backend/a.py"

    payload["mypy_version"] = "different"
    with pytest.raises(ValueError, match="pinned version"):
        deserialize(payload)

    payload["mypy_version"] = "1.19.1"
    payload["error_count"] = 99
    with pytest.raises(ValueError, match="error_count"):
        deserialize(payload)


def test_exact_baseline_comparison_exposes_added_and_removed_errors() -> None:
    old = Fingerprint("backend/example.py", "return", "Missing return")
    new = Fingerprint("backend/example.py", "arg-type", "Bad argument")
    baseline = Counter({old: 1})
    current = Counter({new: 1})

    added = current - baseline
    removed = baseline - current

    assert list(format_changes("New", added)) == [
        "New (1):",
        "  1x backend/example.py [arg-type] Bad argument",
    ]
    assert list(format_changes("Removed", removed)) == [
        "Removed (1):",
        "  1x backend/example.py [return] Missing return",
    ]
