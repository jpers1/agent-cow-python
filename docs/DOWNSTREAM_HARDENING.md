# Downstream hardening scope

## Purpose

This maintained fork extends the upstream `agent-cow` library with bounded
PostgreSQL correctness, isolation, and integration improvements. It remains a
generic copy-on-write engine and is not the SLAIF Agent-State product.

The sequence described here is provisional. Each change requires its own
narrow work order, regression evidence, compatibility review, and
human-approved merge. Implementation status is recorded in the item-specific
sections below.

## PostgreSQL subsystem

The downstream fork intends to improve:

- deterministic operation ordering;
- schema-safe internal SQL and object references;
- privilege-boundary ergonomics;
- fail-closed session operation modes;
- safer transaction and promotion APIs;
- conflict-detection primitives;
- safe integration examples;
- documentation of required PostgreSQL role separation.

Hardening should use PostgreSQL-native semantics, make unsafe preconditions
explicit, and preserve upstream behavior where doing so does not compromise a
documented correctness boundary.

## Blob subsystem

The existing blob subsystem remains upstream-derived code but is not currently
part of the SLAIF Agent-State integration target.

It remains in the repository for upstream compatibility and possible generic
use. Its inclusion does not make it an approved SLAIF media architecture, and
this governance change neither modifies nor removes it.

## Compatibility

The goal is to preserve upstream public APIs where reasonably possible.
Additive configuration, explicit strict modes, and narrowly scoped primitives
are preferred when they can provide the required guarantees. Any breaking API
or behavior change requires explicit human approval and clear migration
documentation.

## Provisional implementation sequence

```text
H01 — deterministic operation ordering
H02 — schema-qualified internal registry/functions
H03 — privilege and role hardening
H04 — safe session transaction API
H05 — safe server-owned session integration examples
H06 — conflict-detection support
H07 — transaction-safe promotion API
H08 — PostgreSQL compatibility/version matrix
H09 — downstream dependency/test cleanup
```

### H01 implementation status and design

H01 uses one schema-qualified PostgreSQL sequence named
`_cow_operation_order_seq` per COW-enabled schema. Every change row stores its
assigned value in `_cow_order`; all COW tables in the schema therefore share a
single monotonic order domain. Trigger conflict-update paths also consume and
store a new value. Rollback gaps are expected and do not affect correctness.

`_cow_updated_at` remains human-readable timestamp metadata. Overlay, commit,
dependency, and scoring paths use `_cow_order` whenever they need causal
ordering.

The sequence is owned by the schema owner and is dropped when the last ordered
COW changes table in the schema is torn down. H03 removes runtime sequence
access in a hardened schema by running the generated write triggers with the
controlled setup owner's privileges.

Deploying the downstream functions automatically upgrades an enabled
upstream-format COW table only when its legacy changes table is empty. If
pending legacy rows exist, deployment fails before replacing functions because
their historical order cannot be reconstructed safely. Applications must
commit or discard those rows with the previous version before retrying the
upgrade.

### H02 implementation status and design

H02 places deployed control functions in the dedicated `agentcow` schema and
qualifies every call to those functions. Each COW-enabled application schema
owns its own `cow_dirty_tables` registry, `_cow_operation_order_seq`, changes
tables, overlay views, and generated trigger functions. Generated SQL uses
quoted, schema-qualified identifiers, and deployed functions run with
`search_path = pg_catalog`.

Agent-cow internal PostgreSQL objects are therefore explicitly
schema-resolved and do not depend on application-controlled `search_path`.
Temporary or attacker-schema objects with the same names cannot redirect dirty
tracking, commit, discard, dependency discovery, or teardown. Existing
`public` installations retain their registry in `public`; H01-era registry
entries for a non-public application schema are moved transactionally into
that schema when the enabled table is redeployed.

H03 removes H02's temporary `PUBLIC` compatibility grants and defines the
ownership, `USAGE`, `EXECUTE`, table, and sequence boundaries described below.

### H03 implementation status and design

H03 adds `harden_cow_schema(...)` for caller-supplied setup, runtime, and
reviewer roles. Hardened runtime roles receive CRUD only on COW views. The
generated write triggers become setup-owned `SECURITY DEFINER` functions with
locked `pg_catalog` search paths and fully qualified application objects, so
runtime roles need no direct change-table, registry, or sequence privilege.

Reviewers receive view `SELECT` plus a narrow set of controlled inspection,
dependency, commit, and discard functions. Setup and teardown remain
owner-only invoker operations. The control schema and all deployed functions
revoke default `PUBLIC` authority.

Writes now fail closed by default when either transaction-local session or
operation context is missing or malformed. The historical canonical
write-through behavior requires the explicit
`allow_unsafe_canonical_writes=True` compatibility option and is not permitted
by the hardened role model.

`validate_cow_schema_privileges(...)` checks effective privileges through
direct grants, `PUBLIC`, ownership, inheritance, and every role reachable with
`SET ROLE`. Unsafe inherited access is reported rather than revoked from an
unlisted role. Apply hardening in an explicit transaction and roll it back if
validation fails. The full deployment works with a non-superuser setup owner.

Session UUID selection remains a trusted-application responsibility: a shared
runtime role can set custom PostgreSQL GUC values. Database credentials and
session selection must remain inside the trusted gateway. See
[`POSTGRES_SECURITY_MODEL.md`](POSTGRES_SECURITY_MODEL.md).

### H04 implementation status and design

H04 adds transaction-owning `asyncpg_cow_session(...)` and
`sqlalchemy_cow_session(...)` scopes. Each scope validates UUID inputs, obtains
one physical connection, rejects an already active transaction, begins one
explicit transaction, rejects stale PostgreSQL COW context, applies and checks
transaction-local context, and commits or rolls back before checking the
connection is clean. Pool release happens only after that lifecycle completes.

The yielded `CowSession` implements the low-level `Executor` shape while also
providing `validate_context()`, `set_operation()`, `set_visible_operations()`,
and explicit `rollback()`. Statements executed through it are preceded by a
context check. A generated operation UUID is used when trusted application
code omits one. `native` remains available for asyncpg- or SQLAlchemy-specific
work, but is explicitly a trusted escape hatch: the library cannot prevent
arbitrary SQL in the application process from changing custom GUCs.

The original `Executor`, `apply_cow_variables(...)`, and raw statement helpers
remain available for compatibility and administrative/reviewer operations.
They do not own a physical connection or transaction and are not the preferred
runtime request path.

### H05 implementation status and design

H05 makes the hardened asyncpg path the primary public integration pattern.
The recommended example separates setup, runtime, and reviewer credentials;
resolves opaque external authorization through application-owned state to a
server-selected UUID; runs CRUD through `asyncpg_cow_session(...)`; and keeps
promotion behind the transaction-owning reviewer API.

The former HTTP-header parser example was removed because parsing a
client-supplied session UUID was not a useful authorization boundary. The
SQLAlchemy example now imports its optional dependency lazily and uses only
`sqlalchemy_cow_session(...)` for runtime context. Low-level context helpers
remain documented as advanced caller-managed APIs, while unsafe canonical
write compatibility is explicitly limited to trusted canonical workflows.

### H06 implementation status and design

H06 adds row-level, first-touch optimistic conflict detection. The first COW
write by a session to a primary key records whether the canonical row existed,
the complete canonical row as `jsonb`, and a signature of the base-table
columns. Every later operation on that key in the same session retains the
original baseline. This is not a database snapshot taken when the session UUID
is created.

Promotion defaults to `conflict_policy="error"`. The commit functions take a
`SHARE ROW EXCLUSIVE` lock on the canonical and changes tables, compare the
current canonical row with the stored baseline, and apply the selected latest
state before releasing that transaction-level lock. A concurrent canonical
writer therefore cannot slip between validation and mutation. Conflicts raise
SQLSTATE `40001`, leave canonical state untouched, and retain pending rows for
review. The explicit `conflict_policy="overwrite"` option preserves historical
last-writer-wins behavior for compatibility.

`get_cow_conflicts(...)` gives the reviewer a controlled, structured view of
row-created, row-deleted, row-changed, and schema-changed conflicts without
granting access to changes tables. It is advisory; commit always repeats the
authoritative check. Selective commit requires a causal prefix for each key and
rebases surviving later operations onto the state accepted by that same
session. Selective discard retains the original baseline.

The comparison is state-based. If canonical state changes and later returns
exactly to the stored row and schema, H06 does not report a historical-write
conflict. H07 supplies the transaction-owning multi-table boundary; the H06
low-level helpers still require caller-managed transaction correctness.

An empty H05 changes table is upgraded automatically. Deployment refuses an
enabled table with pending pre-H06 rows because their first-touch baseline
cannot be reconstructed truthfully. Pending sessions must be committed or
discarded with the previous version before deployment. Existing hardened
schemas must run `harden_cow_schema(...)` again in the deployment transaction
so reviewer roles receive the new controlled conflict and commit signatures.

### H07 implementation status and design

H07 adds `asyncpg_cow_reviewer(...)` and the optional
`sqlalchemy_cow_reviewer(...)`. Each scope pins one physical connection,
rejects an existing transaction or stale runtime GUC context, starts one
transaction, permits inspection followed by one terminal promotion/discard
action, and commits only after mutation and cleanup finish. Conflict,
constraint, injected SQL, exception, and cancellation paths roll back under
shielded cleanup before a pooled connection is released.

`CowReviewer` supports whole-session and selective `commit_*`/`discard_*`
methods. It returns `PromotionResult` or `DiscardResult`, and maps conflict
SQLSTATE `40001` to `CowConflictError` with structured conflict details when
available. Duplicate commit/discard, unknown session, and crossed terminal
requests are successful no-op results when no pending COW state exists.

Runtime writes take a shared transaction-scoped advisory lock for their schema
and session; the controlled `_cow_lock_session` function takes the matching
exclusive reviewer lock, then locks every dirty canonical and changes table in
deterministic name order before inspection or mutation. New same-session work
therefore waits until review finishes instead of changing the promoted set.
Schema-wide commits then retain H06's FK-aware upsert/delete phases and
authoritative locked conflict checks; schema-wide selective operations also
enforce global dependency closure. `READ COMMITTED` is sufficient because
these explicit transaction-level locks protect the complete promotion set.
Unrelated table sets can proceed independently; overlapping sets serialize at
table granularity.

Low-level `commit_cow_*` and `discard_cow_*` helpers remain additive APIs for
advanced callers, but multi-statement use requires one caller-owned physical
connection and transaction. Existing hardened deployments must deploy the H07
functions and rerun `harden_cow_schema(...)` so reviewer roles receive the two
new controlled helper grants.

### H08 implementation status and design

H08 verifies a bounded downstream support window of Python 3.10–3.14 and
PostgreSQL 14–18. The deliberate ten-pair matrix runs the complete 147-test
PostgreSQL tree across every PostgreSQL major on Python 3.12, every Python
minor on PostgreSQL 18, and the oldest boundary pair. Official patch-pinned
PostgreSQL and Python containers make the local result reproducible without a
host PostgreSQL port or hosted service.

`scripts/test_postgres_matrix.py` automates isolated startup, readiness,
fresh installation, full-suite execution, compile/import checks, result
capture, and cleanup. GitHub Actions runs the same matrix for pull requests,
pushes to `main`, and manual dispatch, plus boundary/primary package builds.
The exact versions and evidence are maintained in
[`SUPPORT_MATRIX.md`](SUPPORT_MATRIX.md).

The inherited PostgreSQL fixture stack remains an explicitly recorded LGPL
development limitation. H08 adds no project dependency and does not weaken or
replace that test evidence; permissive-only fixture cleanup remains H09.

This ordering may change after review, but work should remain PR-sized. Tests
for a hardening item should be introduced with its corresponding fix rather
than publishing private audit artifacts independently.
