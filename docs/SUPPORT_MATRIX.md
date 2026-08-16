# PostgreSQL support matrix

## Scope

This matrix covers the hardened downstream PostgreSQL subsystem, including all
H01 through H07 correctness, privilege, session, conflict, and atomic-promotion
tests. It does not make a support claim for the upstream-derived blob backend.

The downstream support window is deliberately finite:

- Python 3.10 through 3.14;
- PostgreSQL 14 through 18;
- asyncpg 0.31.0 for the preferred adapter evidence;
- SQLAlchemy 2.0.46 for the optional async adapter evidence.

Versions outside those windows are not implied to work merely because package
metadata or an upstream dependency permits them.

## Claims before H08

Before H08, `pyproject.toml` declared `requires-python = ">=3.10"` without an
upper bound and listed classifiers for Python 3.10, 3.11, and 3.12. The public
PostgreSQL guide said PostgreSQL 14+ without recording a complete downstream
version matrix. Optional dependency declarations allowed asyncpg 0.29+ and
SQLAlchemy 2.0+, but those ranges were declarations rather than tested adapter
matrices.

H08 narrows the Python metadata to `>=3.10,<3.15`, adds the verified 3.13 and
3.14 classifiers, and replaces the open-ended PostgreSQL statement with the
verified 14–18 range. The asyncpg and SQLAlchemy dependency constraints remain
source-compatible ranges; the exact adapter versions tested here are the only
ones covered by this H08 evidence.

## Matrix strategy

The local and CI matrix uses Python 3.12 as the primary Python version and
PostgreSQL 18 as the representative PostgreSQL version:

1. run the complete 147-test PostgreSQL tree on Python 3.12 against every
   PostgreSQL major from 14 through 18;
2. run the same complete tree on every Python minor from 3.10 through 3.14
   against PostgreSQL 18;
3. add the oldest boundary, Python 3.10 with PostgreSQL 14;
4. build both wheel and sdist and install the wheel in a fresh environment on
   Python 3.10, 3.12, and 3.14.

This produces ten unique full-suite pairs rather than a 25-pair Cartesian
product. Every run includes the H01–H07 regression files; no test is selected
out or skipped by version.

## Python

| Version | Status | Local full-suite evidence |
| --- | --- | --- |
| 3.9 and earlier | UNSUPPORTED | Below the package lower bound |
| 3.10.21 | SUPPORTED | 147 passed on PostgreSQL 14.24 and 18.6; wheel/sdist build and isolated wheel install passed |
| 3.11.16 | SUPPORTED | 147 passed on PostgreSQL 18.6 |
| 3.12.14 | SUPPORTED | 147 passed on PostgreSQL 14.24, 15.19, 16.15, 17.11, and 18.6; wheel/sdist build and isolated wheel install passed |
| 3.13.15 | SUPPORTED | 147 passed on PostgreSQL 18.6 |
| 3.14.7 | SUPPORTED | 147 passed on PostgreSQL 18.6; wheel/sdist build and isolated wheel install passed |
| 3.15 and later | NOT_TESTED | Outside the bounded downstream metadata |

## PostgreSQL

The evidence below was produced on 2026-08-16 using official, patch-pinned
container images. Counts are `passed / failed / skipped`.

| Major | Status | Exact image | `server_version` | Python | Result | Pytest duration |
| --- | --- | --- | --- | --- | --- | --- |
| 13 and earlier | UNSUPPORTED | — | — | — | Below the downstream minimum | — |
| 14 | SUPPORTED | `postgres:14.24` | `14.24 (Debian 14.24-1.pgdg13+2)` | 3.10.21 | 147 / 0 / 0 | 110.800 s |
| 14 | SUPPORTED | `postgres:14.24` | `14.24 (Debian 14.24-1.pgdg13+2)` | 3.12.14 | 147 / 0 / 0 | 150.773 s |
| 15 | SUPPORTED | `postgres:15.19` | `15.19 (Debian 15.19-1.pgdg13+2)` | 3.12.14 | 147 / 0 / 0 | 108.131 s |
| 16 | SUPPORTED | `postgres:16.15` | `16.15 (Debian 16.15-1.pgdg13+2)` | 3.12.14 | 147 / 0 / 0 | 110.763 s |
| 17 | SUPPORTED | `postgres:17.11` | `17.11 (Debian 17.11-1.pgdg13+2)` | 3.12.14 | 147 / 0 / 0 | 70.594 s |
| 18 | SUPPORTED | `postgres:18.6` | `18.6 (Debian 18.6-1.pgdg13+2)` | 3.10.21 | 147 / 0 / 0 | 73.066 s |
| 18 | SUPPORTED | `postgres:18.6` | `18.6 (Debian 18.6-1.pgdg13+2)` | 3.11.16 | 147 / 0 / 0 | 72.409 s |
| 18 | SUPPORTED | `postgres:18.6` | `18.6 (Debian 18.6-1.pgdg13+2)` | 3.12.14 | 147 / 0 / 0 | 63.057 s |
| 18 | SUPPORTED | `postgres:18.6` | `18.6 (Debian 18.6-1.pgdg13+2)` | 3.13.15 | 147 / 0 / 0 | 63.320 s |
| 18 | SUPPORTED | `postgres:18.6` | `18.6 (Debian 18.6-1.pgdg13+2)` | 3.14.7 | 147 / 0 / 0 | 67.262 s |
| 19 and later | NOT_TESTED | — | — | — | Outside the current PostgreSQL release window | — |

## Hardened coverage

The 147 tests include:

- H01 schema-wide sequence and deterministic same-transaction ordering;
- H02 control-schema qualification and hostile `search_path` behavior;
- H03 `SECURITY DEFINER` triggers and setup/runtime/reviewer privilege checks;
- H04 asyncpg pooling, transaction ownership, cancellation, and context cleanup;
- H05 importable safe integration examples;
- H06 JSONB first-touch baselines, locked conflict enforcement, SQLSTATE
  behavior, and selective rebasing;
- H07 advisory locks, concurrent reviewers, multi-table atomic promotion, and
  failure/cancellation rollback;
- the optional SQLAlchemy async session and reviewer adapters.

No `MERGE`-specific test exists in this tree, so H08 does not use PostgreSQL 18
support to imply a tested `MERGE` guarantee.

## Reproducing locally

The maintained runner uses official containers, exposes no host PostgreSQL
port, creates deterministic version-specific container names, refuses to
replace an existing same-named container, and removes its containers and
anonymous volumes after success or failure:

```bash
python scripts/test_postgres_matrix.py
```

When Docker requires passwordless sudo:

```bash
python scripts/test_postgres_matrix.py \
  --container-command "sudo -n docker"
```

Run one pair or one axis entry with:

```bash
python scripts/test_postgres_matrix.py --pair 3.10/14
python scripts/test_postgres_matrix.py --postgres 16
python scripts/test_postgres_matrix.py --python 3.13
```

`--preserve-on-failure` leaves only the failed pair's two labeled containers
for debugging. `--summary-json PATH` writes the concise result records used to
maintain this document.

## CI

The `PostgreSQL matrix` workflow runs on pull requests, pushes to `main`, and
manual dispatch. It runs the same ten full-suite combinations plus package
build/install jobs on Python 3.10, 3.12, and 3.14. A green workflow therefore
means the complete downstream PostgreSQL tree—not only the original upstream
tests—passed on every configured pair.

The primary development pair is Python 3.12/PostgreSQL 18. The oldest supported
pair is Python 3.10/PostgreSQL 14; the newest is Python 3.14/PostgreSQL 18.

## Test dependency note

H08 adds no project dependency. The inherited development path already uses
`pytest-postgresql` and its Psycopg dependency, which are LGPL-licensed and do
not meet the downstream permissive-only target for the eventual standard test
path. H08 keeps those historical dependencies only to execute the existing
147-test evidence without weakening it. Replacing that fixture layer with
permissive asyncpg/direct-container tooling remains H09 scope.

The production package continues to have zero mandatory Python runtime
dependencies. The preferred optional asyncpg 0.31.0 adapter is Apache-2.0 and
the tested optional SQLAlchemy 2.0.46 adapter is MIT.
