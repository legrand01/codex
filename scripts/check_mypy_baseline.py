"""Enforce an exact, reviewable baseline for repository-wide mypy debt."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "mypy-baseline.json"
EXPECTED_MYPY_VERSION = "1.19.1"
MYPY_COMMAND = (
    sys.executable,
    "-m",
    "mypy",
    "--show-error-codes",
    "--no-error-summary",
    "backend",
    "host_agent",
)
ERROR_PATTERN = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: "
    r"error: (?P<message>.*?)(?:\s+\[(?P<code>[^\]]+)\])?$"
)


@dataclass(frozen=True, order=True)
class Fingerprint:
    path: str
    code: str
    message: str


def normalize_path(raw_path: str, root: Path = ROOT) -> str:
    path = Path(raw_path)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            pass
    return path.as_posix()


def parse_errors(output: str, root: Path = ROOT) -> Counter[Fingerprint]:
    errors: Counter[Fingerprint] = Counter()
    for line in output.splitlines():
        match = ERROR_PATTERN.match(line)
        if match is None:
            continue
        errors[
            Fingerprint(
                path=normalize_path(match.group("path"), root),
                code=match.group("code") or "unclassified",
                message=match.group("message"),
            )
        ] += 1
    return errors


def serialize(errors: Counter[Fingerprint]) -> dict[str, Any]:
    fingerprints = [
        {
            "path": fingerprint.path,
            "code": fingerprint.code,
            "message": fingerprint.message,
            "count": count,
        }
        for fingerprint, count in sorted(errors.items())
    ]
    return {
        "schema_version": 1,
        "mypy_version": EXPECTED_MYPY_VERSION,
        "command": [
            "python",
            "-m",
            "mypy",
            "--show-error-codes",
            "--no-error-summary",
            "backend",
            "host_agent",
        ],
        "error_count": sum(errors.values()),
        "fingerprints": fingerprints,
    }


def deserialize(payload: dict[str, Any]) -> Counter[Fingerprint]:
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported mypy baseline schema")
    if payload.get("mypy_version") != EXPECTED_MYPY_VERSION:
        raise ValueError(
            f"mypy baseline must use pinned version {EXPECTED_MYPY_VERSION}"
        )
    raw_fingerprints = payload.get("fingerprints")
    if not isinstance(raw_fingerprints, list):
        raise ValueError("mypy baseline fingerprints must be a list")

    errors: Counter[Fingerprint] = Counter()
    for item in raw_fingerprints:
        if not isinstance(item, dict):
            raise ValueError("invalid mypy baseline fingerprint")
        path = item.get("path")
        code = item.get("code")
        message = item.get("message")
        count = item.get("count")
        if (
            not isinstance(path, str)
            or not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            raise ValueError("invalid mypy baseline fingerprint fields")
        errors[Fingerprint(path=path, code=code, message=message)] += count

    if payload.get("error_count") != sum(errors.values()):
        raise ValueError("mypy baseline error_count does not match fingerprints")
    return errors


def format_changes(
    label: str,
    changes: Counter[Fingerprint],
    limit: int = 20,
) -> Iterable[str]:
    if not changes:
        return ()
    lines = [f"{label} ({sum(changes.values())}):"]
    for fingerprint, count in sorted(changes.items())[:limit]:
        lines.append(
            f"  {count}x {fingerprint.path} [{fingerprint.code}] "
            f"{fingerprint.message}"
        )
    remaining = len(changes) - limit
    if remaining > 0:
        lines.append(f"  ... and {remaining} more fingerprint(s)")
    return lines


def run_mypy() -> tuple[int, str]:
    result = subprocess.run(
        MYPY_COMMAND,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def detect_mypy_version() -> tuple[str | None, str]:
    result = subprocess.run(
        (sys.executable, "-m", "mypy", "--version"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    parts = output.split()
    if result.returncode != 0 or len(parts) < 2:
        return None, output
    return parts[1], output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--update",
        action="store_true",
        help="replace the baseline with the current reviewed mypy output",
    )
    args = parser.parse_args()

    detected_version, version_output = detect_mypy_version()
    if detected_version != EXPECTED_MYPY_VERSION:
        print(
            f"Expected mypy {EXPECTED_MYPY_VERSION}, found "
            f"{version_output or 'an unavailable mypy installation'}",
            file=sys.stderr,
        )
        return 2

    returncode, output = run_mypy()
    if returncode not in {0, 1}:
        print(output, file=sys.stderr)
        print(f"mypy failed to execute successfully (exit {returncode})", file=sys.stderr)
        return 2

    current = parse_errors(output)
    if returncode == 1 and not current:
        print(output, file=sys.stderr)
        print("mypy reported errors but none matched the baseline parser", file=sys.stderr)
        return 2

    baseline_path = args.baseline.resolve()
    if args.update:
        baseline_path.write_text(
            json.dumps(serialize(current), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"Updated {baseline_path.relative_to(ROOT)} with "
            f"{sum(current.values())} mypy error(s)"
        )
        return 0

    try:
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("mypy baseline root must be an object")
        baseline = deserialize(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid mypy baseline: {exc}", file=sys.stderr)
        return 2

    added = current - baseline
    removed = baseline - current
    if added or removed:
        for line in format_changes("New or changed errors", added):
            print(line, file=sys.stderr)
        for line in format_changes("Resolved or changed baseline errors", removed):
            print(line, file=sys.stderr)
        print(
            "Mypy baseline changed. Fix the regression, or review the complete "
            "change and run scripts/check_mypy_baseline.py --update.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Mypy baseline matched exactly: {sum(current.values())} known error(s), "
        "zero unreviewed changes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
