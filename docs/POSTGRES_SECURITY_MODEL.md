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
| Reviewer | Read COW views for a selected session; use controlled inspection, dependency, commit, and discard functions | Direct base/change/registry DML, runtime view DML, setup, teardown, object replacement |

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
)

await deploy_cow_functions(setup_executor)
await enable_cow_schema(setup_executor, schema="content")
await harden_cow_schema(
    setup_executor,
    schema="content",
    runtime_roles=["application_runtime"],
    reviewer_roles=["application_reviewer"],
)
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
  inspection, dependency, commit, and discard functions;
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

Writes to a COW view fail unless both values are valid UUIDs in the current
transaction:

```sql
SET LOCAL app.session_id = '...';
SET LOCAL app.operation_id = '...';
```

The preferred downstream API owns this contract for asyncpg connections/pools
and SQLAlchemy async engines/connections/sessions/factories:

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
  -> SET LOCAL server-owned session and operation IDs
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
hardened role model.

## Trusted-gateway boundary

Custom PostgreSQL settings are application-controlled state, not credentials.
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
