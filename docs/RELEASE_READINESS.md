# Downstream release readiness

## Decision

The downstream `0.2.0rc1` release candidate has completed the final H10 gate:

```text
READY_WITH_DOCUMENTED_LIMITATIONS
```

This is a bounded dependency-readiness assessment, not formal verification,
certification, or a claim of universal production security. It means the
maintained downstream fork is ready for integration as a pinned dependency in
applications that respect the database-role, transaction, session-identity,
migration, support, and trust-boundary requirements documented here.

## Provenance and release identity

- Upstream project: `trail-ml/agent-cow-python`
- Upstream audited commit: `d49d74e3f357d67bf5eda5377bbca50cdf3d952e`
- Upstream package version at that commit: `0.1.7`
- Maintained downstream project: `jpers1/agent-cow-python`
- Downstream release-candidate version: `0.2.0rc1`
- License: MIT, with upstream history, authorship, notices, and `LICENSE`
  preserved

The distribution name remains `agent-cow`. This release candidate is not
published to PyPI. Installing `agent-cow` by unqualified PyPI name may resolve
the upstream package rather than this fork; downstream consumers must use a
reviewed wheel or a pinned fork revision until the human lead approves a
publication mechanism.

## Supported downstream scope

`agentcow.postgres` is the downstream-supported and hardened subsystem. The
verified matrix is Python 3.10 through 3.14 and PostgreSQL 14 through 18. The
preferred integration uses asyncpg 0.31.0; the optional SQLAlchemy async
adapter is verified with SQLAlchemy 2.0.46. Exact images, patch releases, test
counts, and durations are in [`SUPPORT_MATRIX.md`](SUPPORT_MATRIX.md).

`agentcow.blob` remains upstream-derived functionality and is outside the
hardened downstream PostgreSQL support scope assessed by H01–H10. This is not
a general statement that the blob subsystem is broken.

## Reconciled hardening state

H01 through H09 are implemented, regression-tested, and publicly documented:

- deterministic schema-wide operation ordering;
- explicit internal schema and search-path-independent object resolution;
- setup/runtime/reviewer role separation and effective privilege validation;
- fail-closed hardened writes plus transaction-owning runtime session scopes;
- server-owned-session integration examples;
- first-touch optimistic conflict detection with conflict-safe mutation;
- atomic transaction-owning reviewer promotion and discard;
- Python/PostgreSQL matrix CI and local OCI runner; and
- a permissive-only supported development, test, build, and CI environment.

The item-by-item reconciliation is maintained in
[`DOWNSTREAM_HARDENING.md`](DOWNSTREAM_HARDENING.md).

## Recommended deployment and integration

The production boundary uses three caller-supplied PostgreSQL roles:

- the setup/owner role deploys functions, enables/disables CoW, owns protected
  internals, and applies hardening;
- the runtime role performs CRUD only through CoW views with complete
  transaction-local context; and
- the reviewer role uses controlled inspection, conflict, commit, and discard
  functions without direct internal-table DML.

The recommended request path is:

```text
trusted gateway selects a server-owned session UUID
→ acquire one runtime connection
→ asyncpg_cow_session(...)
→ explicit transaction and validated SET LOCAL context
→ controlled application CRUD
→ commit/rollback and clean pool return
```

The recommended promotion path is:

```text
human/application authorizes promotion
→ acquire one reviewer connection
→ asyncpg_cow_reviewer(...)
→ conflict-safe schema-wide mutation and cleanup in one transaction
→ commit or complete rollback
```

Hardened promotion defaults to `conflict_policy="error"`. The explicit
`conflict_policy="overwrite"` option exists only for deliberately chosen
last-writer-wins compatibility. Runtime canonical write-through similarly
requires explicit `allow_unsafe_canonical_writes=True` configuration and is
not suitable for agent-facing traffic.

## Recommended public API inventory

The release-candidate integration test verifies that each name below is
exported by `agentcow.postgres`:

| Area | Recommended entry points |
| --- | --- |
| Deploy and enable | `deploy_cow_functions`, `enable_cow`, `enable_cow_schema` |
| Role boundary | `harden_cow_schema`, `validate_cow_schema_privileges` |
| Runtime asyncpg | `asyncpg_cow_session`, `CowSession` |
| Runtime SQLAlchemy | `sqlalchemy_cow_session`, `CowSession` |
| Conflict review | `get_cow_conflicts`, `CowConflictError` |
| Reviewer asyncpg | `asyncpg_cow_reviewer`, `CowReviewer` |
| Reviewer SQLAlchemy | `sqlalchemy_cow_reviewer`, `CowReviewer` |

The adapter-specific scopes own the required physical connection and explicit
transaction. Low-level executor, context, commit, and discard helpers remain
available for advanced callers, but those callers own transaction and
connection-lifetime correctness.

## Upgrade and migration boundary

Deployment scans existing CoW-enabled tables before replacing functions:

- a database with no enabled CoW tables can deploy and enable normally;
- an upstream `0.1.7` enabled table with an empty changes table is migrated to
  deterministic-order and conflict-baseline metadata;
- upstream `0.1.7` pending changes are refused before migration because their
  truthful causal order and first-touch baseline cannot be reconstructed;
- empty H01/H05-era downstream tables receive the missing metadata;
- pending H01/H05-era rows with ambiguous history are also refused; and
- an existing H06 non-public dirty registry is migrated without losing
  conflict-aware pending rows.

Operators must commit or discard ambiguous pending changes with the prior
version, then deploy `0.2.0rc1`, reapply `harden_cow_schema(...)`, validate the
effective role boundary, and only then resume runtime traffic.

## Release-gate evidence

Evidence gathered on 2026-08-16:

| Check | Result |
| --- | --- |
| Focused H10 readiness tests | PASSED — 3 passed, 0 failed, 0 skipped on PostgreSQL 18.6 |
| Complete local PostgreSQL suite | PASSED — 150 passed, 0 failed, 0 skipped on Python 3.12.3/PostgreSQL 18.6 |
| Ten-pair local version matrix | PASSED — 150 passed, 0 failed, 0 skipped in every pair |
| Actual upstream `0.1.7` upgrade: no CoW | PASSED — downstream deploy and enable succeeded |
| Actual upstream `0.1.7` upgrade: enabled/empty | PASSED — ordering and conflict metadata migrated |
| Actual upstream `0.1.7` upgrade: pending | PASSED — migration refused before schema mutation and pending row remained |
| H01/H05/intermediate migration regressions | PASSED — 5 passed, 0 failed, 0 skipped |
| Hardened lifecycle and conflict demonstration | PASSED — accepted changes promoted, discarded changes stayed non-canonical, conflict preserved canonical and pending state |
| Multi-table failure demonstration | PASSED — later constraint failure rolled back prior mutation, preserved pending state, and left the connection reusable |
| Clean-clone dependency policy and full suite | PASSED — fresh clone and fresh uv cache; policy approved 15 distributions; 150 passed, 0 failed, 0 skipped on Python 3.12.3/PostgreSQL 18.6 |
| Python 3.10/3.12/3.14 wheel, sdist, install, and archive inspection | PASSED — each isolated wheel reported `0.2.0rc1`; inspected wheel had 48 entries and sdist had 71; no forbidden names or obvious key/credential content |
| GitHub Actions dependency, ten matrix, and three package jobs | PASSED — 14 of 14 jobs on code-bearing commit `225325cdf9542927a19dfc0b14c67a43d3305c6e` in [run 31945072011](https://github.com/jpers1/agent-cow-python/actions/runs/31945072011) |

The matrix uses official `postgres:14.24`, `15.19`, `16.15`, `17.11`, and
`18.6` images and patch-pinned Python 3.10.21, 3.11.16, 3.12.14, 3.13.15, and
3.14.7 images. Every pair runs the same complete 150-test tree.

## Dependency and release policy

The canonical supported environment is installed with:

```bash
uv sync --frozen --group dev
uv run python scripts/check_dependency_policy.py
```

It contains no mandatory third-party runtime dependency and the installed
development graph is checked against the repository's permissive-only
allowlist. The standard path excludes Psycopg, pytest-postgresql, mirakuru,
Black, Hatchling, pathspec, Moto, and certifi. The detailed direct/transitive
inventory is in [`DEPENDENCY_INVENTORY.md`](DEPENDENCY_INVENTORY.md).

The inherited automatic PyPI workflow and tag-pushing release script are not
part of this release candidate. The fork retains build/test automation but has
no automatic PyPI publication path. Downstream publishing, tagging, or GitHub
Release creation requires a separate human-authorized work order and future
explicit configuration.

## Known limitations

- Session identity is trusted application state. PostgreSQL custom GUCs are
  not an authentication mechanism, and database credentials must remain
  inside the trusted gateway.
- A trusted application process with arbitrary SQL can intentionally mutate
  its own GUC context; the library does not claim a cryptographic binding
  between a shared database role and one external session.
- Conflict detection compares first-touch baseline state with current
  canonical state. It does not record every historical mutation; a row that
  changes and later returns exactly to its baseline is non-conflicting.
- Promotion uses table and advisory locks to favor correctness over maximal
  concurrency.
- Ambiguous pending changes from pre-ordering or pre-conflict schemas are
  intentionally not migratable in place.
- Application schema changes while relevant changes remain pending fail
  conservatively when a safe comparison cannot be made.
- The supported windows are Python 3.10–3.14 and PostgreSQL 14–18; versions
  outside them are not covered by this evidence.
- The blob subsystem is outside the hardened downstream PostgreSQL support
  scope.
