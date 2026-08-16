#!/usr/bin/env python3
"""Run the complete PostgreSQL suite in disposable OCI containers.

The default matrix exercises the PostgreSQL axis on the primary Python version,
the Python axis on the newest PostgreSQL version, and both boundary pairs.  No
host port is published: each Python container shares only its paired PostgreSQL
container's network namespace.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

POSTGRES_IMAGES = {
    "14": "postgres:14.24",
    "15": "postgres:15.19",
    "16": "postgres:16.15",
    "17": "postgres:17.11",
    "18": "postgres:18.6",
}
PYTHON_IMAGES = {
    "3.10": "python:3.10.21-slim",
    "3.11": "python:3.11.16-slim",
    "3.12": "python:3.12.14-slim",
    "3.13": "python:3.13.15-slim",
    "3.14": "python:3.14.7-slim",
}
PRIMARY_PYTHON = "3.12"
PRIMARY_POSTGRES = "18"

UV_VERSION = "0.12.5"


@dataclass(frozen=True)
class Pair:
    python: str
    postgres: str


@dataclass
class Result:
    python: str
    postgres: str
    python_image: str
    postgres_image: str
    status: str
    duration_seconds: float
    server_version: str | None = None
    python_version: str | None = None
    asyncpg_version: str | None = None
    sqlalchemy_version: str | None = None
    tests: int | None = None
    failures: int | None = None
    errors: int | None = None
    skipped: int | None = None
    pytest_seconds: float | None = None


def default_pairs() -> list[Pair]:
    pairs = {
        *(Pair(PRIMARY_PYTHON, version) for version in POSTGRES_IMAGES),
        *(Pair(version, PRIMARY_POSTGRES) for version in PYTHON_IMAGES),
        Pair(min(PYTHON_IMAGES), min(POSTGRES_IMAGES)),
        Pair(max(PYTHON_IMAGES), max(POSTGRES_IMAGES)),
    }
    return sorted(pairs, key=lambda pair: (pair.postgres, pair.python))


class ContainerClient:
    def __init__(self, command: str) -> None:
        self.command = shlex.split(command)
        if not self.command:
            raise ValueError("container command cannot be empty")

    def run(
        self,
        *args: str,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.command, *args],
            check=check,
            text=True,
            capture_output=capture_output,
        )

    def exists(self, name: str) -> bool:
        result = self.run(
            "container", "inspect", name, check=False, capture_output=True
        )
        return result.returncode == 0

    def remove(self, name: str) -> None:
        self.run("container", "rm", "--force", "--volumes", name, check=False)


def parse_pair(value: str) -> Pair:
    try:
        python, postgres = value.split("/", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pair must use PYTHON/POSTGRES") from exc
    if python not in PYTHON_IMAGES:
        raise argparse.ArgumentTypeError(f"unsupported Python minor: {python}")
    if postgres not in POSTGRES_IMAGES:
        raise argparse.ArgumentTypeError(f"unsupported PostgreSQL major: {postgres}")
    return Pair(python, postgres)


def wait_until_healthy(client: ContainerClient, name: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.run(
            "inspect",
            "--format",
            "{{.State.Health.Status}}",
            name,
            check=False,
            capture_output=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "healthy":
            return
        time.sleep(1)
    client.run("logs", name, check=False)
    raise RuntimeError(f"{name} did not become healthy within {timeout} seconds")


def read_junit(path: Path) -> dict[str, int | float]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    return {
        "tests": sum(int(suite.attrib.get("tests", 0)) for suite in suites),
        "failures": sum(int(suite.attrib.get("failures", 0)) for suite in suites),
        "errors": sum(int(suite.attrib.get("errors", 0)) for suite in suites),
        "skipped": sum(int(suite.attrib.get("skipped", 0)) for suite in suites),
        "pytest_seconds": sum(float(suite.attrib.get("time", 0.0)) for suite in suites),
    }


def test_command() -> str:
    return f"""set -eu
cp -a /source /tmp/agent-cow-postgresql
cd /tmp/agent-cow-postgresql
python -m pip install --disable-pip-version-check --quiet uv=={UV_VERSION}
uv sync --frozen --group dev
export PATH="/tmp/agent-cow-postgresql/.venv/bin:$PATH"
python scripts/check_dependency_policy.py
python - <<'PY' > /result/environment.json
import asyncio
import json
import platform

import asyncpg
import sqlalchemy

async def server_version():
    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=5432,
        user="postgres",
        password="postgres",
        database="postgres",
    )
    try:
        return await connection.fetchval("SHOW server_version")
    finally:
        await connection.close()

print(json.dumps({{
    "python_version": platform.python_version(),
    "server_version": asyncio.run(server_version()),
    "asyncpg_version": asyncpg.__version__,
    "sqlalchemy_version": sqlalchemy.__version__,
}}))
PY
cat /result/environment.json
python -m pytest agentcow/postgres/tests/ -v --junitxml=/result/pytest.xml
python -m compileall -q agentcow
python -c 'import agentcow; print(agentcow.__version__)'
"""


def run_pair(
    client: ContainerClient,
    pair: Pair,
    source: Path,
    *,
    pull: bool,
    preserve_on_failure: bool,
    pulled: set[str],
) -> Result:
    python_image = PYTHON_IMAGES[pair.python]
    postgres_image = POSTGRES_IMAGES[pair.postgres]
    suffix = f"pg{pair.postgres}-py{pair.python.replace('.', '')}"
    postgres_name = f"agentcow-matrix-{suffix}-postgres"
    python_name = f"agentcow-matrix-{suffix}-python"

    for name in (postgres_name, python_name):
        if client.exists(name):
            raise RuntimeError(
                f"refusing to replace existing container {name}; remove it explicitly"
            )

    if pull:
        for image in (postgres_image, python_image):
            if image not in pulled:
                client.run("pull", image)
                pulled.add(image)

    started = time.monotonic()
    success = False
    environment: dict[str, str] = {}
    junit: dict[str, int | float] = {}
    print(
        f"\n=== Python {pair.python} / PostgreSQL {pair.postgres} "
        f"({python_image}, {postgres_image}) ===",
        flush=True,
    )

    try:
        client.run(
            "run",
            "--detach",
            "--name",
            postgres_name,
            "--label",
            "org.agentcow.test-matrix=true",
            "--env",
            "POSTGRES_PASSWORD=postgres",
            "--env",
            "POSTGRES_DB=postgres",
            "--health-cmd",
            "pg_isready -U postgres -d postgres",
            "--health-interval",
            "1s",
            "--health-timeout",
            "5s",
            "--health-retries",
            "60",
            postgres_image,
        )
        wait_until_healthy(client, postgres_name)

        with tempfile.TemporaryDirectory(prefix=f"agentcow-{suffix}-") as result_dir:
            result_path = Path(result_dir)
            completed = client.run(
                "run",
                "--name",
                python_name,
                "--label",
                "org.agentcow.test-matrix=true",
                "--network",
                f"container:{postgres_name}",
                "--mount",
                f"type=bind,source={source},target=/source,readonly",
                "--mount",
                f"type=bind,source={result_path},target=/result",
                "--env",
                "PG_HOST=127.0.0.1",
                "--env",
                "PG_PORT=5432",
                "--env",
                "PG_USER=postgres",
                "--env",
                "PG_PASSWORD=postgres",
                python_image,
                "sh",
                "-c",
                test_command(),
                check=False,
            )
            if (result_path / "environment.json").exists():
                environment = json.loads((result_path / "environment.json").read_text())
            if (result_path / "pytest.xml").exists():
                junit = read_junit(result_path / "pytest.xml")
            success = completed.returncode == 0
    finally:
        if success or not preserve_on_failure:
            client.remove(python_name)
            client.remove(postgres_name)
        else:
            print(
                f"preserved failed containers: {python_name}, {postgres_name}",
                file=sys.stderr,
            )

    return Result(
        python=pair.python,
        postgres=pair.postgres,
        python_image=python_image,
        postgres_image=postgres_image,
        status="PASSED" if success else "FAILED",
        duration_seconds=round(time.monotonic() - started, 3),
        **environment,
        **junit,
    )


def selected_pairs(args: argparse.Namespace) -> list[Pair]:
    if args.pair:
        return sorted(set(args.pair), key=lambda pair: (pair.postgres, pair.python))
    pairs: set[Pair] = set()
    for postgres in args.postgres:
        if postgres not in POSTGRES_IMAGES:
            raise ValueError(f"unsupported PostgreSQL major: {postgres}")
        pairs.add(Pair(PRIMARY_PYTHON, postgres))
    for python in args.python:
        if python not in PYTHON_IMAGES:
            raise ValueError(f"unsupported Python minor: {python}")
        pairs.add(Pair(python, PRIMARY_POSTGRES))
    return (
        sorted(pairs, key=lambda pair: (pair.postgres, pair.python)) or default_pairs()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--container-command",
        default=os.environ.get("CONTAINER_COMMAND", "docker"),
        help="OCI command, including a sudo prefix when needed",
    )
    parser.add_argument("--pair", action="append", type=parse_pair, default=[])
    parser.add_argument("--postgres", action="append", default=[])
    parser.add_argument("--python", action="append", default=[])
    parser.add_argument("--no-pull", action="store_true")
    parser.add_argument("--preserve-on-failure", action="store_true")
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    pairs = selected_pairs(args)
    if args.list:
        for pair in pairs:
            print(
                f"Python {pair.python} ({PYTHON_IMAGES[pair.python]}) / "
                f"PostgreSQL {pair.postgres} ({POSTGRES_IMAGES[pair.postgres]})"
            )
        return 0

    client = ContainerClient(args.container_command)
    client.run("version")
    source = Path(__file__).resolve().parents[1]
    pulled: set[str] = set()
    results: list[Result] = []

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)

    try:
        for pair in pairs:
            result = run_pair(
                client,
                pair,
                source,
                pull=not args.no_pull,
                preserve_on_failure=args.preserve_on_failure,
                pulled=pulled,
            )
            results.append(result)
            print(json.dumps(asdict(result), sort_keys=True), flush=True)
            if result.status != "PASSED":
                break
    except KeyboardInterrupt:
        print("matrix interrupted", file=sys.stderr)
        return 130
    finally:
        if args.summary_json:
            args.summary_json.parent.mkdir(parents=True, exist_ok=True)
            args.summary_json.write_text(
                json.dumps([asdict(result) for result in results], indent=2) + "\n"
            )

    return (
        0
        if len(results) == len(pairs)
        and all(result.status == "PASSED" for result in results)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
