# PostgreSQL security model

## Purpose

The hardened PostgreSQL path separates deployment, application CRUD, and
promotion authority. Role names are supplied by the application; agent-cow
does not create globally named roles.

The model assumes one setup/owner role owns the `agentcow` control schema and
each enabled base table and has `USAGE`/`CREATE` on the application schema.
That role may be an ordinary non-superuser role. It needs `CREATE` on the
database only when it must create the control or application schema.

## Roles

| Role | Allowed | Not allowed |
| --- | --- | --- |
| Setup / owner | Deploy functions, enable or disable COW, own protected objects, apply hardening | Delegation to an untrusted request path |
| Runtime | `SELECT`, `INSERT`, `UPDATE`, and `DELETE` through enabled COW views | Direct base/change/registry access, sequence access, management functions, schema object replacement |
| Reviewer | Read COW views for a selected session; use controlled operation, dependency, conflict, commit, and discard functions | Direct base/change/registry DML, runtime view DML, setup, teardown, object replacement |

`PUBLIC` receives no access to the `agentcow` schema and no function execute
authority. Generated trigger functions also revoke `PUBLIC EXECUTE`.

## Configuration

Create the roles and application objects as normal, then deploy and enable COW
as the setup owner. Apply the boundary in the same explicit transaction:

```python
from agentcow.postgres import (
    deploy_cow_functions,
    enable_cow_schema,
    harden_cow_schema,
    validate_cow_schema_privileges,
)

await deploy_cow_functions(setup_executor)
await enable_cow_schema(setup_executor, schema="content")
await harden_cow_schema(
    setup_executor,
    schema="content",
    runtime_roles=["application_runtime"],
    reviewer_roles=["application_reviewer"],
)
validation = await validate_cow_schema_privileges(
    setup_executor,
    schema="content",
    runtime_roles=["application_runtime"],
    reviewer_roles=["application_reviewer"],
)
if not validation["safe"]:
    raise RuntimeError(validation["violations"])
```

The caller controls the transaction boundary because the driver-neutral
`Executor` protocol exposes only `execute(sql)`. If validation fails, roll back
the transaction and correct the reported grant or ownership problem.
Run hardening again in the same administrative transaction whenever the set of
COW-enabled tables or assigned roles changes, before runtime traffic resumes.

Hardening performs these operations for the explicitly listed roles:

- revokes direct privileges on `*_base`, `*_changes`, `cow_dirty_tables`, and
  `_cow_operation_order_seq`;
- grants runtime CRUD only on the COW views;
- grants reviewers `SELECT` on the views and `EXECUTE` only on controlled
  inspection, dependency, conflict, commit, and discard functions;
- removes runtime and reviewer `CREATE` on the application and control
  schemas;
- removes `PUBLIC` access to managed internal objects and functions;
- regenerates write triggers as setup-owned `SECURITY DEFINER` functions with
  `search_path = pg_catalog` and explicitly qualified application objects;
- records the schema-specific role assignment used by controlled functions.

The API does not revoke privileges from arbitrary roles inherited by a runtime
or reviewer. Instead, effective-privilege validation follows every role the
caller can reach with `SET ROLE` and rejects the configuration if any path can
access protected objects. This avoids silently changing unrelated role policy.

`validate_cow_schema_privileges(...)` may also be run independently after
role or grant changes. It checks effective table, sequence, function, schema,
ownership, `SECURITY DEFINER`, locked `search_path`, and `PUBLIC` privileges.

## Request contract

At the PostgreSQL layer, writes to a COW view fail unless valid session and
operation UUID settings exist in the current transaction. Applications should
not issue those context statements manually. The preferred downstream API owns
the contract for asyncpg connections/pools and SQLAlchemy async
engines/connections/sessions/factories:

```python
from agentcow.postgres import asyncpg_cow_session

async with asyncpg_cow_session(
    application_pool,
    session_id=server_selected_session_id,
) as cow:
    await cow.execute(
        "INSERT INTO content.pages (id, title) VALUES (1, 'Draft')"
    )
    await cow.set_operation()  # select a new generated logical operation
    await cow.execute(
        "UPDATE content.pages SET title = 'Revised' WHERE id = 1"
    )
```

The equivalent `sqlalchemy_cow_session(...)` scope accepts an async engine,
connection, session, or `async_sessionmaker`; its `cow.native` property exposes
the owned SQLAlchemy object for ORM work. A normal scope exit commits the
request transaction. Exceptions, cancellation, or `await cow.rollback()` roll
it back.

Each high-level scope enforces:

```text
acquire connection
  -> begin transaction
  -> reject stale session, operation, or visibility context
  -> apply server-owned transaction-local session and operation IDs
  -> validate the applied values from PostgreSQL
  -> application CRUD through COW views
  -> validate context before library-controlled statements and scope exit
  -> commit or roll back
  -> verify no usable COW context remains
  -> return connection
```

Passing an already transactional connection is rejected because a nested
savepoint cannot provide the required physical-connection lifetime boundary.
If `operation_id` is omitted, the high-level API generates a UUID. Multiple
logical actions may use `await cow.set_operation(...)`; visibility changes use
`await cow.set_visible_operations(...)` and are applied and verified as local
settings.

`Executor`, `apply_cow_variables(...)`, and the statement builders remain
low-level APIs. They cannot prove connection identity or transaction lifetime,
so callers using them must enforce the entire contract themselves. Raw `SET
LOCAL` in autocommit mode is unsupported for safe COW writes.

Missing, reset, expired, or malformed write context raises an error. A normal
`SELECT` without context continues to show canonical state. The historical
canonical-write-through-view behavior is available only through the explicit
`allow_unsafe_canonical_writes=True` enable option and is incompatible with the
hardened role model. That option exists for trusted canonical application
workflows that intentionally preserve upstream write-through semantics. It is
not appropriate for agent-facing runtime traffic.

## Trusted-gateway boundary

Agent-cow does not authenticate external users or capabilities. The
application must select the session UUID after authorization. Custom
PostgreSQL settings are application-controlled state, not credentials.
The hardened role prevents direct access to internal state, but it does not
cryptographically bind a shared runtime database role to one session UUID.

Database credentials therefore belong to a trusted semantic gateway and must
never be exposed to an agent. The gateway must derive `session_id` and
`operation_id` from server-owned state; an external caller must not choose
arbitrary values. Capability-token lookup and request authentication belong in
SLAIF Agent-State, not this generic library.

`CowSession.execute()` detects unexpected context mutation before its next
statement, and the scope checks again before commit. `cow.native` cannot make
arbitrary SQL trustworthy: code with direct access can issue `RESET`, change a
custom GUC, or bypass wrapper validation. H03 still makes missing/reset write
context fail closed, but the application process and its database credentials
remain inside the trusted boundary.

## Conflict and promotion contract

Conflict baselines are captured automatically by the generated write trigger
when a session first changes a primary key. The baseline contains:

- whether the canonical row existed;
- the complete canonical row represented as `jsonb`;
- a signature of the canonical table's visible columns.

Later operations in that session retain the first baseline even when they use
different operation UUIDs. This is first-touch row detection, not a database
snapshot at session creation and not a substitute for an application's
optional session-wide revision policy.

Reviewers should use the transaction-owning scope, which supports inspection
without internal-table access and makes the subsequent action atomic:

```python
from agentcow.postgres import asyncpg_cow_reviewer

async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
    conflicts = await reviewer.conflicts(
        trusted_session_id, schema="content"
    )
    result = await reviewer.commit_session(
        trusted_session_id, schema="content"
    )
```

Each conflict record identifies the table, primary key, latest relevant
operation and one of `BASE_ROW_CHANGED`, `BASE_ROW_DELETED`,
`BASE_ROW_CREATED`, or `BASE_SCHEMA_CHANGED`. Inspection is advisory. It does
not authorize a later commit and cannot replace the check inside promotion.

Commit uses `conflict_policy="error"` by default. Each per-table promotion
locks canonical and pending state against concurrent canonical DML, validates
the stored baseline, and mutates while the lock is held. A conflict raises
SQLSTATE `40001`; canonical state and pending session rows are preserved when
the caller rolls back the failed transaction. Historical last-writer-wins
behavior is available only through the explicit
`conflict_policy="overwrite"` compatibility option.

Selective commit accepts only a causal operation prefix for each affected
primary key. After accepting a prefix, later pending operations from the same
session are rebased to the newly accepted canonical state. Selective discard
does not refresh their baseline. A base-table schema change while work is
pending produces a conservative schema conflict.

The comparison detects current-state divergence, not every mutation event. If
canonical row and schema state change and then return exactly to the stored
baseline, promotion is allowed. The table lock also means conflicting
promotion serializes canonical writers at table granularity; this favors a
generic correctness guarantee over maximum writer concurrency.

The high-level reviewer scope pins one connection, begins one explicit
transaction, takes a session-scoped advisory lock, locks the complete dirty
table set in deterministic name order, and includes FK-ordered mutation plus
cleanup in that transaction. Runtime COW writes take the matching shared lock,
so new work for that schema/session waits until review ends. A conflict
or later table/constraint/cleanup failure rolls back earlier canonical writes
and preserves all pending state. Cancellation rollback is shielded before a
pooled connection is released. Duplicate terminal requests with no pending
work return structured no-op results.

Selective commit/discard is also schema-wide and atomic. The locked operation
set must be causally closed: a selected commit cannot omit a pending
predecessor, and a discard cannot leave a pending dependent whose predecessor
was removed. Surviving later operations keep H06 rebase semantics.

PostgreSQL's default `READ COMMITTED` isolation is sufficient for this API
because H06/H07 explicitly lock every affected canonical and changes table for
the transaction. Overlapping reviewer actions serialize at table granularity;
unrelated table sets do not acquire common locks.

`CowConflictError` is the stable Python conflict contract. Constraint and
other database failures retain their adapter-native exception types, while
invalid selections and stale connection state use distinct agent-cow
exceptions. The low-level commit/discard functions remain available, but a
caller using them must pin one connection and own one explicit transaction.

When upgrading an existing hardened schema to H06, first remove pending work
using the previous version. Deploy H06 and rerun `harden_cow_schema(...)` in
the same administrative transaction. The commit function signatures change to
carry the explicit policy, and the reviewer must receive the new controlled
grants before traffic resumes.

## Patterns not recommended for hardened deployments

- Accepting an external request's session UUID as database identity without a
  server-side authorization lookup.
- Using raw transaction-local context statements in autocommit mode.
- Giving a runtime role direct internal-table access or promotion authority.
- Sharing privileged database credentials with an agent-facing process.
- Enabling unsafe canonical-write compatibility for agent-facing traffic.
- Exposing arbitrary SQL through `CowSession.native` to an external caller.

The complete public asyncpg example is
[`asyncpg_safe_session_example.py`](../agentcow/postgres/examples/asyncpg_safe_session_example.py).
It demonstrates setup, effective-privilege validation, runtime pool use,
server-owned session resolution, conflict inspection, and reviewer-only
conflict-safe atomic promotion.
