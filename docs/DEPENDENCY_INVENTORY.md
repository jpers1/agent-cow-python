# Downstream dependency inventory

## Scope and method

This is the H09 inventory for the supported PostgreSQL development, test,
build, and CI path. Versions are from `uv.lock`; dependency paths are from the
locked distribution metadata and clean H08 matrix environments. License
identifiers are engineering-policy classifications based on distribution
metadata and project license files, not legal advice.

The automated installed-environment check is
`scripts/check_dependency_policy.py`. It rejects any distribution outside the
reviewed allowlist and explicitly reports the historical excluded packages.

## Build system

| Distribution | Version | Relationship | Purpose/path | License/SPDX | Policy |
| --- | --- | --- | --- | --- | --- |
| `setuptools` | 80.10.2 | Direct build backend and direct `dev` tool | `[build-system]`; installed in the checked environment so `--no-isolation` builds use the reviewed backend | MIT | ALLOWED |
| `build` | 1.5.0 | Direct `dev` tool | PEP 517 wheel/sdist frontend | MIT | ALLOWED |
| `packaging` | 26.0 | Transitive | `build -> packaging`; also used by pytest | Apache-2.0 OR BSD-2-Clause | ALLOWED |
| `pyproject-hooks` | 1.2.0 | Transitive | `build -> pyproject-hooks` | MIT | ALLOWED |
| `tomli` | 2.4.0 | Conditional transitive | `build/pytest -> tomli` on Python 3.10 | MIT | ALLOWED |
| `colorama` | 0.4.6 | Conditional transitive | `build/pytest -> colorama` on Windows | BSD-3-Clause | ALLOWED |
| `importlib-metadata` | 9.0.0 | Conditional transitive | `build -> importlib-metadata` on Python 3.10.0–3.10.1 | Apache-2.0 | ALLOWED |
| `zipp` | 4.1.0 | Conditional transitive | `importlib-metadata -> zipp` | MIT | ALLOWED |

Setuptools replaces Hatchling. Hatchling itself is permissive, but its build
dependency graph includes MPL-2.0 `pathspec`, which does not satisfy this
repository's stricter policy. Package name, version source, package discovery,
README package data, wheel creation, and sdist creation are represented in
`pyproject.toml` without a new runtime dependency.

## Mandatory runtime

| Distribution | Version | Relationship | Purpose/path | License/SPDX | Policy |
| --- | --- | --- | --- | --- | --- |
| `agent-cow` | 0.2.0rc1 | Project | PostgreSQL CoW library | MIT | ALLOWED |

There are zero mandatory third-party Python runtime dependencies.

## SQLAlchemy optional runtime

| Distribution | Version | Relationship | Purpose/path | License/SPDX | Policy |
| --- | --- | --- | --- | --- | --- |
| `asyncpg` | 0.31.0 | Direct optional | `agent-cow[sqlalchemy]`; preferred PostgreSQL driver | Apache-2.0 | ALLOWED |
| `async-timeout` | 5.0.1 | Conditional transitive | `asyncpg -> async-timeout` on Python 3.10 | Apache-2.0 | ALLOWED |
| `SQLAlchemy` | 2.0.46 | Direct optional | Async ORM adapter | MIT | ALLOWED |
| `greenlet` | 3.3.1 | Platform transitive | `SQLAlchemy -> greenlet` where supported | MIT AND Python-2.0 | ALLOWED |
| `typing-extensions` | 4.15.0 | Transitive | `SQLAlchemy -> typing-extensions` | PSF-2.0 | ALLOWED |

## PostgreSQL development and test

| Distribution | Version | Relationship | Purpose/path | License/SPDX | Policy |
| --- | --- | --- | --- | --- | --- |
| `pytest` | 9.0.2 | Direct `dev` | Complete 150-test runner | MIT | ALLOWED |
| `pytest-asyncio` | 1.3.0 | Direct `dev` | Async fixtures/tests | Apache-2.0 | ALLOWED |
| `asyncpg` | 0.31.0 | Direct `dev` | Fixture database access and production-adapter tests | Apache-2.0 | ALLOWED |
| `SQLAlchemy` | 2.0.46 | Direct `dev` | Optional adapter tests | MIT | ALLOWED |
| `iniconfig` | 2.3.0 | Transitive | `pytest -> iniconfig` | MIT | ALLOWED |
| `packaging` | 26.0 | Transitive | `pytest -> packaging` | Apache-2.0 OR BSD-2-Clause | ALLOWED |
| `pluggy` | 1.6.0 | Transitive | `pytest -> pluggy` | MIT | ALLOWED |
| `Pygments` | 2.19.2 | Transitive | `pytest -> pygments` | BSD-2-Clause | ALLOWED |
| `exceptiongroup` | 1.3.1 | Conditional transitive | `pytest -> exceptiongroup` on Python 3.10 | MIT | ALLOWED |
| `backports-asyncio-runner` | 1.2.0 | Conditional transitive | `pytest-asyncio -> backports-asyncio-runner` on Python 3.10 | PSF-2.0 | ALLOWED |
| `typing-extensions` | 4.15.0 | Conditional/transitive | pytest-asyncio and SQLAlchemy typing compatibility | PSF-2.0 | ALLOWED |

The test harness creates and drops one disposable database per test through
asyncpg. A small asyncpg-backed synchronous facade retains the old fixture's
implicit transaction contract for inherited assertions; native asyncpg pools
and connections continue to exercise the H04–H07 adapter, concurrency, and
cancellation paths.

## Format and check tooling

| Distribution | Version | Relationship | Purpose/path | License/SPDX | Policy |
| --- | --- | --- | --- | --- | --- |
| `ruff` | 0.15.22 | Direct `dev` | Python formatter and checker | MIT | ALLOWED |

Ruff replaces Black because Black's standard dependency graph includes
MPL-2.0 `pathspec`. H09 does not apply a repository-wide formatting rewrite;
developers format and check the Python files they change.

## CI and local orchestration

| Tool | Version/reference | Purpose | License/policy |
| --- | --- | --- | --- |
| `uv` | 0.12.5 | Locked environment bootstrap and sync | MIT OR Apache-2.0; ALLOWED |
| `actions/checkout` | v7 | GitHub checkout action | MIT; ALLOWED |
| `actions/setup-python` | v7 | GitHub Python toolchain action | MIT; ALLOWED |
| Docker/OCI CLI | Local installation | Disposable local container orchestration | No Python dependency; no hosted account required |
| Official Python images | Patch-pinned H08 tags | Isolated Python matrix | No Python package dependency added to project |
| Official PostgreSQL images | 14.24, 15.19, 16.15, 17.11, 18.6 | Disposable database matrix | PostgreSQL is PostgreSQL-licensed; no hosted account required |

GitHub Actions is a CI execution venue, not a required local build/test
service. `scripts/test_postgres_matrix.py` reproduces the supported matrix
locally without a hosted account or secrets.

## Blob optional runtime and test scope

The inherited runtime extra remains separately installable and is not included
by the standard PostgreSQL `dev` group:

| Distribution | Version | Relationship | Purpose/path | License/SPDX | Policy |
| --- | --- | --- | --- | --- | --- |
| `boto3` | 1.42.76 | Direct `blob` optional | Upstream-derived S3 client | Apache-2.0 | ALLOWED, OUTSIDE HARDENED POSTGRESQL SCOPE |
| `botocore` | 1.42.76 | Transitive | `boto3 -> botocore` | Apache-2.0 | ALLOWED, OUTSIDE HARDENED POSTGRESQL SCOPE |
| `jmespath` | 1.1.0 | Transitive | boto3/botocore query support | MIT | ALLOWED, OUTSIDE HARDENED POSTGRESQL SCOPE |
| `s3transfer` | 0.16.0 | Transitive | `boto3 -> s3transfer` | Apache-2.0 | ALLOWED, OUTSIDE HARDENED POSTGRESQL SCOPE |
| `python-dateutil` | 2.9.0.post0 | Transitive | `botocore -> python-dateutil` | Apache-2.0 OR BSD-3-Clause | ALLOWED, OUTSIDE HARDENED POSTGRESQL SCOPE |
| `six` | 1.17.0 | Transitive | `python-dateutil -> six` | MIT | ALLOWED, OUTSIDE HARDENED POSTGRESQL SCOPE |
| `urllib3` | 2.6.3 | Transitive | `botocore -> urllib3` | MIT | ALLOWED, OUTSIDE HARDENED POSTGRESQL SCOPE |

No blob-test dependency is declared in the supported environment. The
upstream-derived blob tests remain in the source tree, but their historical
Moto tooling is not installed or run by the downstream PostgreSQL CI path.
Blob support is outside the hardened downstream PostgreSQL support scope.

## Removed standard-path dependencies

| Distribution/path before H09 | Version observed before H09 | Policy issue | H09 result |
| --- | --- | --- | --- |
| `pytest-postgresql` | 8.0.0 | LGPLv3+ direct PostgreSQL test dependency | Removed; direct asyncpg database lifecycle replaces it |
| `psycopg` | 3.3.2 | LGPL-3.0-only dependency of pytest-postgresql and direct CI install | Removed; asyncpg performs all supported test access |
| `mirakuru` | 3.0.2 | LGPL-3.0-or-later transitive of pytest-postgresql | Removed with pytest-postgresql |
| `black -> pathspec` | 26.1.0 -> 1.0.4 | `pathspec` is MPL-2.0 | Black replaced by Ruff |
| `hatchling -> pathspec` | Unpinned build isolation -> 1.0.4 observed | `pathspec` is MPL-2.0 | Hatchling replaced by Setuptools |
| `moto -> requests -> certifi` | 5.1.22 -> 2.33.0 -> 2026.2.25 | `certifi` is MPL-2.0; blob test stack was also outside target scope | Moto removed from standard development groups |

`uv.lock` no longer contains Black, certifi, Hatchling, mirakuru, Moto,
pathspec, Psycopg, or pytest-postgresql. None is installed by the canonical
PostgreSQL development command.
