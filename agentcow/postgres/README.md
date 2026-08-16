# agent-cow — PostgreSQL

PostgreSQL backend for [agent-cow](../../README.md). Provides database-level Copy-On-Write via PL/pgSQL functions, change-tracking tables, and views — works with any async PostgreSQL driver.

## Installation

```bash
pip install agent-cow
```

Requires Python 3.10+ and PostgreSQL 14+.

For the preferred asyncpg runtime adapter:

```bash
pip install agent-cow asyncpg
```

For SQLAlchemy support:

```bash
pip install agent-cow[sqlalchemy]
```

## Recommended hardened integration

Use three existing PostgreSQL roles with distinct credentials:

| Role | Purpose |
| --- | --- |
| Setup / owner | Own application tables; deploy, enable, harden, and validate COW |
| Runtime | CRUD only through COW views with trusted transaction context |
| Reviewer | Controlled inspection and commit/discard after authorization |

The setup owner can be a non-superuser. A PostgreSQL administrator may be
needed only for initial database and role creation. The example below assumes
the setup role already owns the `content` schema and its application tables.

### 1. Deploy, enable, harden, and validate

```python
import asyncpg

from agentcow.postgres import (
    deploy_cow_functions,
    enable_cow_schema,
    harden_cow_schema,
    validate_cow_schema_privileges,
)
from agentcow.postgres.examples.asyncpg_safe_session_example import AsyncpgExecutor

setup_connection = await asyncpg.connect(SETUP_DATABASE_URL)
try:
    async with setup_connection.transaction():
        setup = AsyncpgExecutor(setup_connection)
        await deploy_cow_functions(setup)
        await enable_cow_schema(
            setup, schema="content", exclude={"alembic_version"}
        )
        await harden_cow_schema(
            setup,
            schema="content",
            runtime_roles=["application_runtime"],
            reviewer_roles=["application_reviewer"],
        )
        validation = await validate_cow_schema_privileges(
            setup,
            schema="content",
            runtime_roles=["application_runtime"],
            reviewer_roles=["application_reviewer"],
        )
        if not validation["safe"]:
            raise RuntimeError(validation["violations"])
finally:
    await setup_connection.close()
```

Hardening revokes runtime access to base tables, changes tables, registries,
sequences, and management functions. It grants runtime CRUD only on the COW
views. See the [security model](../../docs/POSTGRES_SECURITY_MODEL.md) for the
complete privilege contract.

### 2. Resolve application identity and run runtime CRUD

Agent-cow does not authenticate external users or capabilities. The
application must select the session UUID after authorization.

```python
from agentcow.postgres import asyncpg_cow_session

runtime_pool = await asyncpg.create_pool(RUNTIME_DATABASE_URL)

# The capability is opaque transport input. The returned UUID is server-owned.
trusted_session_id = await application_session_store.resolve(external_capability)

async with asyncpg_cow_session(
    runtime_pool,
    session_id=trusted_session_id,
) as cow:
    await cow.execute(
        "INSERT INTO content.pages (id, title) VALUES (1, 'Draft')"
    )
    await cow.set_operation()  # new library-generated logical operation UUID
    await cow.execute(
        "UPDATE content.pages SET title = 'Revised' WHERE id = 1"
    )
```

The pool must authenticate as `application_runtime`, not as the setup or
reviewer role. One connection and one explicit transaction are owned by the
scope. Normal exit commits isolated change rows; exceptions, cancellation, or
`await cow.rollback()` roll them back. The connection is checked for stale or
leaked context before release.

The optional `sqlalchemy_cow_session(...)` adapter provides equivalent
lifecycle guarantees for SQLAlchemy asyncio engines, connections, sessions,
and `async_sessionmaker`. Psycopg and other drivers retain the low-level
`Executor` API but do not have H04 transaction-lifecycle guarantees.

### 3. Inspect and promote with the reviewer role

Promotion follows application or human authorization and uses separate
reviewer credentials:

```python
from agentcow.postgres import (
    CowConflictError,
    asyncpg_cow_reviewer,
)

reviewer_pool = await asyncpg.create_pool(REVIEWER_DATABASE_URL)

async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
    operations = await reviewer.operations(
        trusted_session_id, schema="content"
    )
    dependencies = await reviewer.dependencies(
        trusted_session_id, schema="content"
    )
    if application_or_human_approved:
        try:
            result = await reviewer.commit_session(
                trusted_session_id, schema="content"
            )
        except CowConflictError as conflict:
            show_conflicts_to_reviewer(conflict.conflicts)
            raise
    else:
        result = await reviewer.discard_session(
            trusted_session_id, schema="content"
        )
```

The runtime role cannot inspect internal tables or promote changes. Conflict
inspection is useful for review, but commit independently enforces the stored
first-touch baseline while blocking concurrent canonical DML. The reviewer
scope owns one connection and transaction through mutation, cleanup, and pool
release; any failure rolls the complete action back.

## Optimistic conflicts

The first time a session modifies a primary key, agent-cow records whether the
canonical row existed, its complete row state, and the base-table column
signature. Further operations on that key retain this original baseline. This
is row-level first-touch detection, not a snapshot taken when a session UUID is
created.

Commit is conflict-safe by default. If the canonical row is changed, deleted,
or created after first touch, or the relevant table schema changes, promotion
raises PostgreSQL SQLSTATE `40001`. Canonical state is not overwritten and the
pending rows remain available after the failed transaction is rolled back.
`get_cow_conflicts(...)` returns structured conflict details for reviewer UIs,
but is advisory; promotion always validates again under a table lock.

`CowReviewer.conflicts(...)` supports advisory UI inspection.
`CowReviewer.commit_session(...)` repeats the authoritative checks under the
same transaction's locks and raises `CowConflictError` on conflict.

The comparison is current-state based. A canonical row that changes and then
returns exactly to its baseline is not treated as historically changed. An
application may layer a coarser session-wide revision gate on top when that is
its desired policy.

`conflict_policy="overwrite"` is an explicit compatibility option for legacy
last-writer-wins promotion. Hardened integrations should retain the default
`"error"` policy.

Selective commit accepts only a causal prefix for each key and rebases later
pending operations onto the state just accepted by that same session.
Selective discard preserves the original baseline. The high-level reviewer
methods make full-session and selective actions atomic across every affected
table.

An empty pre-H06 changes table upgrades automatically. Pending pre-H06 rows
cannot be assigned a truthful first-touch baseline, so deployment refuses the
upgrade until they are committed or discarded using the previous version.
After deploying into an existing hardened schema, rerun
`harden_cow_schema(...)` in the same administrative transaction to apply the
new reviewer function grants.

## How It Works

1. **Renames your table** from `users` to `users_base`
2. **Creates a changes table** `users_changes` to store session-specific modifications
3. **Creates a COW view** named `users` that merges base + changes
4. **Your code doesn't change** — queries still target `users` (now a view)

The safe session scope applies trusted transaction-local session and operation
context. Writes go to the changes table, while reads merge canonical state with
that session's changes. Other sessions and canonical readers see only base
data. PostgreSQL custom GUC values are trusted application state, not an
authentication mechanism.

## Web Framework Integration

Transport authentication and capability lookup belong in the application.
Resolve an untrusted request to trusted server-owned UUIDs first, then wrap the
whole database request in one safe scope:

```python
from fastapi import FastAPI, Request
from agentcow.postgres import asyncpg_cow_session

app = FastAPI()

@app.middleware("http")
async def cow_middleware(request: Request, call_next):
    authorized = await resolve_server_owned_cow_context(request)
    async with asyncpg_cow_session(
        request.app.state.database_pool,
        session_id=authorized.session_id,
        operation_id=authorized.operation_id,
    ) as cow:
        request.state.database = cow
        return await call_next(request)
```

Do not treat client-supplied headers, bearer tokens, or route parameters as
session UUIDs. The resolver must authenticate transport input and return a
server-owned database identity.

## Patterns not recommended for hardened deployments

- Passing `session_id` directly from an untrusted HTTP header or request field.
- Using raw `SET LOCAL` in autocommit mode.
- Giving the runtime role direct base-table, changes-table, registry, or
  sequence privileges.
- Sharing setup, runtime, and reviewer credentials or exposing database
  credentials to an agent.
- Enabling `allow_unsafe_canonical_writes=True` for agent-facing traffic.
- Exposing arbitrary SQL or `CowSession.native` to an external agent.

`allow_unsafe_canonical_writes=True` exists only as a compatibility option for
trusted canonical application workflows. It restores the historical behavior
in which a write without active COW context reaches canonical state. It is not
appropriate for an agent-facing runtime and is incompatible with the hardened
role model.

## Advanced low-level integration

`Executor`, `apply_cow_variables(...)`, and
`build_cow_variable_statements(...)` remain available for administrative,
reviewer, and advanced adapter code. They do not acquire a connection, begin a
transaction, validate context, handle cancellation, or clean a pooled
connection. Runtime code should use an H04 safe session scope unless it
independently implements every lifecycle guarantee.

## API Reference

### Setup (one-time)

| Function | Description |
|----------|-------------|
| `deploy_cow_functions(executor)` | Deploy COW PL/pgSQL functions to the database |
| `enable_cow(executor, table_name, *, pk_cols=None, schema="public", allow_deferred_fks=False, allow_unsafe_canonical_writes=False)` | Enable COW on a single table. Missing write context fails closed unless the unsafe compatibility option is explicitly enabled |
| `enable_cow_schema(executor, *, schema="public", exclude=None, allow_deferred_fks=False, allow_unsafe_canonical_writes=False)` | Enable COW on all user tables in a schema. Returns list of enabled table names |
| `harden_cow_schema(executor, schema, runtime_roles, reviewer_roles)` | Apply and validate the setup/runtime/reviewer privilege boundary |
| `validate_cow_schema_privileges(executor, schema, runtime_roles, reviewer_roles)` | Validate effective direct, inherited, ownership, schema, function, table, and sequence privileges |

### Per-Request

| Function | Description |
|----------|-------------|
| `asyncpg_cow_session(connection_or_pool, *, session_id, operation_id=None, visible_operations=None)` | Recommended asyncpg scope: owns one connection and transaction, validates context, and commits/rolls back/cleans automatically |
| `sqlalchemy_cow_session(engine_connection_session_or_factory, *, session_id, operation_id=None, visible_operations=None)` | Equivalent optional SQLAlchemy asyncio scope |
| `apply_cow_variables(executor, session_id, operation_id=None, visible_operations=None)` | Low-level: set COW variables in a transaction managed entirely by the caller |
| `reset_cow_variables(executor)` | Reset all COW session variables to defaults |
| `build_cow_variable_statements(session_id, operation_id=None, visible_operations=None)` | Build raw `SET LOCAL` SQL strings (for use without an executor) |

### Review

| Function | Description |
|----------|-------------|
| `get_session_operations(executor, session_id, *, schema="public")` | List all operation UUIDs in a session |
| `get_operation_dependencies(executor, session_id, *, schema="public")` | Get `(depends_on, operation_id)` pairs for a session |
| `get_cow_conflicts(executor, session_id, *, schema="public", operation_ids=None)` | Inspect structured first-touch conflicts through the controlled reviewer API |
| `asyncpg_cow_reviewer(connection_or_pool)` | Recommended reviewer scope: pins one asyncpg connection and owns the complete promotion/discard transaction |
| `sqlalchemy_cow_reviewer(engine_connection_session_or_factory)` | Equivalent optional SQLAlchemy asyncio reviewer scope |
| `CowReviewer.commit_session(...)` / `discard_session(...)` | Atomically promote or discard every dirty table and return a structured result |
| `CowReviewer.commit_operations(...)` / `discard_operations(...)` | Atomically apply a causally valid operation selection across tables |
| `set_visible_operations(executor, operation_ids)` | Filter which operations' changes are visible in subsequent reads |
| `get_cow_status(executor, *, schema="public")` | Get COW status: deployed functions, enabled tables, changes tables |
| `is_cow_enabled(executor, config, *, schema="public")` | Check if COW is both requested and properly configured |

### Commit / Discard

| Function | Description |
|----------|-------------|
| `commit_cow_session(executor, table_name, session_id, *, pk_cols=None, schema="public", conflict_policy="error")` | Commit all session changes with conflict checking by default; `"overwrite"` explicitly restores last-writer-wins compatibility |
| `commit_cow_session_schema(executor, session_id, *, schema="public", defer_fk_constraints=False, conflict_policy="error")` | Commit every dirty table with conflict checking. Orders by FK dependency by default — see [FK constraints](#fk-constraints-and-multi-table-commits) |
| `discard_cow_session(executor, table_name, session_id, *, schema="public")` | Discard all session changes |
| `commit_cow_operations(executor, table_name, session_id, operation_ids, *, pk_cols=None, schema="public", conflict_policy="error")` | Commit a causally valid operation prefix with conflict checking |
| `discard_cow_operations(executor, table_name, session_id, operation_ids, *, schema="public")` | Discard specific operations |
| `commit_cow_operations_schema(executor, session_id, operation_ids, *, schema="public", conflict_policy="error")` | Low-level schema-wide selective commit; caller must own one transaction |
| `discard_cow_operations_schema(executor, session_id, operation_ids, *, schema="public")` | Low-level schema-wide selective discard; caller must own one transaction |

### Teardown

| Function | Description |
|----------|-------------|
| `disable_cow(executor, table_name, *, schema="public", revert_deferred_fks=True)` | Disable COW on a table, restoring the original base table. Reverts any `DEFERRABLE INITIALLY IMMEDIATE` FKs back to `NOT DEFERRABLE` |
| `disable_cow_schema(executor, *, schema="public", exclude=None, revert_deferred_fks=True)` | Disable COW on all COW-enabled tables in a schema |

## FK Constraints and Multi-Table Commits

When you commit a session that spans multiple tables with foreign keys between them, the commit order matters:

- Parents must be inserted before children (new user → new project referencing that user)
- Children must be deleted before parents (delete project → delete its owner)

By default, `commit_cow_session_schema` discovers the FK graph between your dirty tables, topologically sorts them, and commits in two phases: upserts run parents-first and deletes run children-first. This keeps the database consistent at every step without any schema modifications — your FK constraints stay exactly as you defined them.

If the dirty-table subgraph contains an FK **cycle**, the library raises `ValueError` at commit time. Cycles genuinely can't be ordered row-by-row; you need constraint deferral to commit them.

For cycles, self-referential FKs, or bulk loads where you'd rather rely on Postgres to defer FK checks until end-of-transaction, opt in with:

```python
# One-time: flip the relevant FKs to DEFERRABLE INITIALLY IMMEDIATE
await enable_cow_schema(executor, allow_deferred_fks=True)

# Per-commit: use SET CONSTRAINTS ALL DEFERRED around the loop
await commit_cow_session_schema(executor, session_id, defer_fk_constraints=True)
```

`enable_cow(..., allow_deferred_fks=True)` only flips constraints that are currently `NOT DEFERRABLE`. Constraints that are `INITIALLY DEFERRED` (a deliberate schema choice) are left alone. `disable_cow` reverses the flip.

Without the opt-in, **no schema changes** are made by agent-cow.

### Types

| Type | Description |
|------|-------------|
| `CowSession` | Active high-level scope with `execute`, context validation, operation/visibility switching, explicit rollback, and `native` adapter access |
| `CowReviewer` | Active high-level reviewer scope with inspection plus one atomic terminal action |
| `PromotionResult` / `DiscardResult` | Immutable structured terminal-action outcomes, including tables, operations, pending state, and no-op status |
| `CowConflictError` | Stable Python conflict exception with structured conflict details where available |
| `Executor` | Protocol — any object with `async execute(sql: str) -> list[tuple]` |
| `CowPostgresConfig` | Dataclass with `agent_session_id`, `operation_id`, `visible_operations` fields |
| `CowStatus` | TypedDict with `enabled`, `tables_with_cow`, `changes_tables`, `cow_functions_deployed` fields |
