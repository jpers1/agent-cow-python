#!/usr/bin/env python3
"""Validate the installed supported PostgreSQL development environment."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Approval:
    license: str
    reason: str


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


APPROVED = {
    "agent-cow-postgresql": Approval("MIT", "project under test"),
    "async-timeout": Approval("Apache-2.0", "asyncpg on Python 3.10"),
    "asyncpg": Approval("Apache-2.0", "preferred PostgreSQL adapter"),
    "backports-asyncio-runner": Approval("PSF-2.0", "pytest-asyncio on Python 3.10"),
    "build": Approval("MIT", "PEP 517 build frontend"),
    "colorama": Approval("BSD-3-Clause", "build frontend on Windows"),
    "exceptiongroup": Approval("MIT", "pytest on Python 3.10"),
    "greenlet": Approval("MIT AND Python-2.0", "SQLAlchemy async adapter dependency"),
    "importlib-metadata": Approval(
        "Apache-2.0", "build frontend on Python 3.10.0–3.10.1"
    ),
    "iniconfig": Approval("MIT", "pytest dependency"),
    "packaging": Approval(
        "Apache-2.0 OR BSD-2-Clause", "pytest/build metadata dependency"
    ),
    "pip": Approval("MIT", "standard environment installer"),
    "pluggy": Approval("MIT", "pytest plugin system"),
    "pygments": Approval("BSD-2-Clause", "pytest terminal output dependency"),
    "pyproject-hooks": Approval("MIT", "build frontend dependency"),
    "pytest": Approval("MIT", "test runner"),
    "pytest-asyncio": Approval("Apache-2.0", "async pytest support"),
    "ruff": Approval("MIT", "formatter/checker"),
    "setuptools": Approval("MIT", "build backend"),
    "sqlalchemy": Approval("MIT", "optional adapter coverage"),
    "tomli": Approval("MIT", "build frontend dependency on Python 3.10"),
    "typing-extensions": Approval("PSF-2.0", "adapter typing compatibility"),
    "uv": Approval("MIT OR Apache-2.0", "environment and lock manager"),
    "wheel": Approval("MIT", "wheel build/install tooling"),
    "zipp": Approval("MIT", "importlib-metadata dependency"),
}

REQUIRED = {
    "agent-cow-postgresql",
    "asyncpg",
    "build",
    "pytest",
    "pytest-asyncio",
    "ruff",
    "setuptools",
    "sqlalchemy",
}

EXPLICITLY_DISALLOWED = {
    "agent-cow",
    "black",
    "boto3",
    "botocore",
    "certifi",
    "hatchling",
    "mirakuru",
    "moto",
    "pathspec",
    "psycopg",
    "psycopg-binary",
    "psycopg-pool",
    "pytest-postgresql",
    "s3transfer",
}


def declared_license(distribution: importlib.metadata.Distribution) -> str:
    expression = distribution.metadata.get("License-Expression")
    if expression:
        return expression.strip()
    value = distribution.metadata.get("License")
    if value and "\n" not in value and len(value) <= 120:
        return value.strip()
    return "metadata ambiguous; repository mapping used"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    installed = {
        _name(distribution.metadata["Name"]): distribution
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    failures: list[str] = []

    for name in sorted(installed):
        distribution = installed[name]
        approval = APPROVED.get(name)
        observed = declared_license(distribution)
        if name in EXPLICITLY_DISALLOWED:
            failures.append(f"disallowed package installed: {name}")
            status = "DISALLOWED"
            approved_license = "—"
            reason = "excluded from the maintained project environment"
        elif approval is None:
            failures.append(f"unexpected package installed: {name}")
            status = "UNREVIEWED"
            approved_license = "—"
            reason = "not in the auditable supported-environment allowlist"
        else:
            status = "APPROVED"
            approved_license = approval.license
            reason = approval.reason
        print(
            f"{status}: {name}=={distribution.version}; "
            f"approved={approved_license}; metadata={observed}; reason={reason}"
        )

    missing = sorted(REQUIRED - installed.keys())
    failures.extend(f"required package missing: {name}" for name in missing)

    for name in sorted(EXPLICITLY_DISALLOWED):
        if name not in installed:
            print(f"ABSENT: {name}")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1

    print(
        f"PASS: {len(installed)} installed distributions match the "
        "permissive-only PostgreSQL development allowlist"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
