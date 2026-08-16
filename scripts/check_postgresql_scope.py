#!/usr/bin/env python3
"""Validate the maintained PostgreSQL-only source and installed environment."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DISTRIBUTION = "agent-cow-postgresql"
REMOVED_DISTRIBUTIONS = {
    "agent-cow",
    "boto3",
    "botocore",
    "moto",
    "s3transfer",
}


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def main() -> int:
    failures: list[str] = []
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())

    project_name = normalize_name(project["project"]["name"])
    if project_name != EXPECTED_DISTRIBUTION:
        failures.append(
            f"distribution is {project_name!r}, expected {EXPECTED_DISTRIBUTION!r}"
        )

    extras = project["project"].get("optional-dependencies", {})
    if "blob" in extras:
        failures.append("removed optional dependency extra is still declared")

    package_data = project.get("tool", {}).get("setuptools", {}).get("package-data", {})
    if "agentcow.blob" in package_data:
        failures.append("removed package remains in Setuptools package data")

    if (REPOSITORY_ROOT / "agentcow" / "blob").exists():
        failures.append("removed package directory still exists")

    installed = {
        normalize_name(distribution.metadata["Name"])
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    for name in sorted(REMOVED_DISTRIBUTIONS & installed):
        failures.append(f"removed dependency is installed: {name}")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1

    print(
        "PASS: source, packaging metadata, and installed environment are "
        "PostgreSQL-only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
