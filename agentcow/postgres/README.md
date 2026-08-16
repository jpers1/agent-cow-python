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

## Quick Start

### 1. Create a setup executor

Administrative and review APIs work with any async PostgreSQL driver through
the low-level `Executor` protocol. Wrap a caller-managed connection with an
`async execute(sql) -> list[tuple]` method:

```python
# asyncpg
class AsyncpgExecutor:
    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql: str) -> list[tuple]:
        return [tuple(r) for r in await self._conn.fetch(sql)]


# SQLAlchemy AsyncSession
class SAExecutor:
    def __init__(self, session):
        self._s = session

    async def execute(self, sql: str) -> list[tuple]:
        from sqlalchemy import text
        result = await self._s.execute(text(sql))
        return [tuple(row) for row in result.fetchall()] if result.returns_rows else []
```

### 2. One-time setup

Deploy the COW functions to your database and enable COW on your tables:

```python
from agentcow.postgres import deploy_cow_functions, enable_cow_schema

executor = AsyncpgExecutor(conn)

await deploy_cow_functions(executor)
await enable_cow_schema(executor, schema="public", exclude={"alembic_version"})
# Returns: ["users", "orders", "products", ...]
```

If you only need COW on specific tables:

```python
from agentcow.postgres import enable_cow

await enable_cow(executor, "users")
await enable_cow(executor, "orders")
```

Writes fail closed by default unless both transaction-local COW identifiers
are set. Production deployments should also configure the explicit role
boundary described in
[`docs/POSTGRES_SECURITY_MODEL.md`](../../docs/POSTGRES_SECURITY_MODEL.md).

### 3. Run an agent session

Use the transaction-owning asyncpg API for the preferred runtime path. The
application must select `session_id` from trusted server-side state; never use
an untrusted request value as database identity.

```python
from agentcow.postgres import asyncpg_cow_session

async with asyncpg_cow_session(
    application_pool,
    session_id=server_selected_session_id,
) as cow:
    # One acquired connection and one explicit transaction are already active.
    await cow.execute(
        "INSERT INTO users (name, email) "
        "VALUES ('Bessie', 'bessie@sunnymeadow.farm')"
    )

    # Rotate to another logical action. Omit the UUID to generate one.
    await cow.set_operation()
    await cow.execute(
        "UPDATE users SET email = 'bessie@rollinghills.farm' "
        "WHERE name = 'Bessie'"
    )
```

Normal exit commits the request transaction (the isolated change rows, not a
promotion to canonical state). Exceptions, cancellation, or
`await cow.rollback()` roll it back. The equivalent
`sqlalchemy_cow_session(...)` API supports SQLAlchemy asyncio engines,
connections, sessions, and `async_sessionmaker`.

Both APIs reject stale context, apply and validate transaction-local settings,
and verify the connection is clean before pool release. Production data is
untouched until a separate authorized review operation promotes changes.

Adapter support is intentionally explicit:

- `asyncpg.Connection` and `asyncpg.Pool` are the preferred first-class path;
- SQLAlchemy `AsyncEngine`, `AsyncConnection`, `AsyncSession`, and
  `async_sessionmaker` are supported by the optional SQLAlchemy integration;
- psycopg and other drivers remain compatible with the low-level `Executor`
  API, but do not yet have a transaction-owning high-level adapter. They must
  not be represented as having H04 lifecycle guarantees.

### 4. Review and commit

After the session, inspect what the agent did and selectively commit or discard:

```python
from agentcow.postgres import (
    get_session_operations,
    get_operation_dependencies,
    commit_cow_operations,
    discard_cow_operations,
    commit_cow_session,
)

ops = await get_session_operations(executor, session_id)
deps = await get_operation_dependencies(executor, session_id)
# ops:  [UUID('aaa...'), UUID('bbb...'), UUID('ccc...')]
# deps: [(UUID('aaa...'), UUID('bbb...')), ...]  — bbb depends on aaa

# Cherry-pick: commit the good operations, discard the rest
await commit_cow_operations(executor, "users", session_id, [ops[0]])
await discard_cow_operations(executor, "users", session_id, [ops[1], ops[2]])

# Or commit everything at once
await commit_cow_session(executor, "users", session_id)
```

## How It Works

1. **Renames your table** from `users` to `users_base`
2. **Creates a changes table** `users_changes` to store session-specific modifications
3. **Creates a COW view** named `users` that merges base + changes
4. **Your code doesn't change** — queries still target `users` (now a view)

When you set `app.session_id` and `app.operation_id` via `SET LOCAL`, all writes go to the changes table. Reads automatically merge base data with your session's changes. Other sessions (and production) see only the base data. This all happens at the SQL layer — no application-level query routing required.

See the [interactive demo](https://www.agent-cow.com) for a worked example of a farm inventory management system where an agent makes both good and bad decisions.

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
session UUIDs. The historical header parser is retained only as a clearly
marked compatibility example; it is not a production authorization boundary.

Low-level integrations may still build `SET LOCAL` statements directly:

```python
from agentcow.postgres import build_cow_variable_statements

stmts = build_cow_variable_statements(session_id, operation_id)
# ["SET LOCAL app.session_id = '...'", "SET LOCAL app.operation_id = '...'"]
for stmt in stmts:
    await conn.execute(stmt)
```

That path is responsible for acquiring one physical connection, beginning and
ending one explicit transaction, validating context, handling cancellation,
and cleaning pooled state. Raw `SET LOCAL` in autocommit mode is unsupported
for safe COW writes.

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
| `set_visible_operations(executor, operation_ids)` | Filter which operations' changes are visible in subsequent reads |
| `get_cow_status(executor, *, schema="public")` | Get COW status: deployed functions, enabled tables, changes tables |
| `is_cow_enabled(executor, config, *, schema="public")` | Check if COW is both requested and properly configured |

### Commit / Discard

| Function | Description |
|----------|-------------|
| `commit_cow_session(executor, table_name, session_id, *, pk_cols=None, schema="public")` | Commit all session changes to the base table |
| `commit_cow_session_schema(executor, session_id, *, schema="public", defer_fk_constraints=False)` | Commit every dirty table for the session. Orders by FK dependency by default — see [FK constraints](#fk-constraints-and-multi-table-commits) |
| `discard_cow_session(executor, table_name, session_id, *, schema="public")` | Discard all session changes |
| `commit_cow_operations(executor, table_name, session_id, operation_ids, *, pk_cols=None, schema="public")` | Commit specific operations (cherry-pick) |
| `discard_cow_operations(executor, table_name, session_id, operation_ids, *, schema="public")` | Discard specific operations |

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
| `Executor` | Protocol — any object with `async execute(sql: str) -> list[tuple]` |
| `CowPostgresConfig` | Dataclass with `agent_session_id`, `operation_id`, `visible_operations` fields |
| `CowStatus` | TypedDict with `enabled`, `tables_with_cow`, `changes_tables`, `cow_functions_deployed` fields |
