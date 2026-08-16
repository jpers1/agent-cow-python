# agent-cow

**Database Copy-On-Write for AI agent workspace isolation**

[![PyPI](https://img.shields.io/pypi/v/agent-cow.svg)](https://pypi.org/project/agent-cow/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Downstream fork

This repository is a downstream fork of
[`trail-ml/agent-cow-python`](https://github.com/trail-ml/agent-cow-python),
originally developed by Trail.

The upstream project and this fork are distributed under the MIT license. This
fork preserves upstream history and attribution while adding PostgreSQL
correctness, isolation, and integration improvements required by downstream
applications including SLAIF Agent-State.

Upstream project: https://github.com/trail-ml/agent-cow-python

`agent-cow` isolates application database writes in a PostgreSQL copy-on-write
layer until a separate reviewer accepts or discards them.

## Hardened PostgreSQL integration

The downstream recommended path is:

```text
trusted application
  -> hardened runtime role
  -> asyncpg pool
  -> asyncpg_cow_session(...)
  -> server-owned session UUID
  -> controlled CRUD through COW views
```

Start with the [PostgreSQL guide](./agentcow/postgres/), then use the
[security model](./docs/POSTGRES_SECURITY_MODEL.md) to configure separate
setup, runtime, and reviewer roles. Agent-cow does not authenticate external
users or capabilities. The application must select the session UUID after
authorization.

> Read the full article: [Copy-on-Write in Agentic Systems](https://www.trail-ml.com/blog/agent-cow)

```
Without agent-cow:                With agent-cow:

┌───────┐       ┌──────────┐     ┌───────┐     ┌──────┐     ┌──────────┐
│ Agent │──────>│ Database │     │ Agent │────>│ COW  │────>│ Database │
└───────┘       └──────────┘     └───────┘     │ View │     └──────────┘
 writes directly                               └──────┘
 to production                                   writes go to changes table
                                                 reads merge base + changes
                                                 user reviews, then commits or discards
```

## Installation

```bash
pip install agent-cow
```

Requires Python 3.10+.

## How It Works

1. **Renames your table** from `users` to `users_base`
2. **Creates a changes table** `users_changes` to store session-specific modifications
3. **Creates a COW view** named `users` that merges base + changes
4. **Your code doesn't change** — queries still target `users` (now a view)

The recommended session API applies server-selected transaction-local context,
routes writes into the changes table, and merges those changes into reads for
that session. Other sessions and canonical readers see only base data.

<details>
<summary><strong>Why Copy-on-Write for agents?</strong></summary>

Alignment is an open problem in AI safety, and [misalignment during agent execution may not always be obvious](https://www.cold-takes.com/why-ai-alignment-could-be-hard-with-modern-deep-learning/). At best, a misaligned agent is annoying (i.e. if the agent does something other than what the user wants it to do) and at worst, dangerous (i.e. leading to sensitive data loss, tool misuse, and [other harms](https://www.anthropic.com/research/agentic-misalignment)). Rather than tackling the alignment problem directly, this repo focuses on minimizing potential harm a misaligned agent can cause.

- **Changes can be reviewed at the end of a session**, rather than needing to repeatedly 'accept' each action as it is executed. This minimizes the direct human supervision required while improving the safeguards in place.
- Mistakes are less consequential, since the **agent can't write directly to the main/production data**. If some changes are good but others aren't, users can cherry-pick operations they wish to keep.
- **Misalignment patterns become more visible**. When reviewing changes at the end of a session, users can clearly identify where the agent deviated from intended behavior and adjust the system prompt or agent configuration accordingly to prevent similar issues in future sessions.
- **Multiple agents or agent sessions** can run simultaneously on isolated copies without interfering with each other.
</details>

## Backends
| Backend | Docs | Status |
|---------|------|--------|
| **PostgreSQL** | [agentcow/postgres](./agentcow/postgres/) | Hardened downstream path |
| **pg-lite (TypeScript)** | [agent-cow-typescript](https://github.com/trail-ml/agent-cow-ts) | Available |
| **Blob/File Storage** | — | Upstream-derived; not a SLAIF integration target |

## Quick Example (PostgreSQL)

```python
import asyncpg

from agentcow.postgres import asyncpg_cow_session

# Authorization and capability lookup are application responsibilities.
trusted_session_id = await application_session_store.resolve(external_capability)
runtime_pool = await asyncpg.create_pool(RUNTIME_DATABASE_URL)

try:
    async with asyncpg_cow_session(
        runtime_pool,
        session_id=trusted_session_id,
    ) as cow:
        await cow.execute("INSERT INTO content.pages (id, title) VALUES (1, 'Draft')")
finally:
    await runtime_pool.close()
```

The pool authenticates as the hardened runtime role. Setup and promotion use
separate roles and controlled APIs. See the [PostgreSQL docs](./agentcow/postgres/)
for the complete deployment, runtime, and reviewer example.

## API Reference

### Core Functions

- `deploy_cow_functions(executor)` — Deploy COW SQL functions (one-time setup)
- `enable_cow(executor, table_name)` — Enable COW on a table
- `enable_cow_schema(executor)` — Enable COW on all tables in a schema
- `disable_cow(executor, table_name)` — Disable COW and restore original table
- `disable_cow_schema(executor)` — Disable COW on all tables in a schema
- `commit_cow_session(executor, table_name, session_id)` — Commit all session changes
- `discard_cow_session(executor, table_name, session_id)` — Discard all session changes
- `get_cow_status(executor)` — Get COW status for a schema

### Advanced and review functions

- `apply_cow_variables(executor, session_id, operation_id)` — Advanced low-level caller-managed transaction helper
- `get_session_operations(executor, session_id)` — List all operations in a session
- `get_operation_dependencies(executor, session_id)` — Get operation dependency graph
- `commit_cow_operations(executor, table_name, session_id, operation_ids)` — Commit specific operations
- `discard_cow_operations(executor, table_name, session_id, operation_ids)` — Discard specific operations

### Session Management

- `asyncpg_cow_session(connection_or_pool, session_id=...)` — Recommended transaction-owning asyncpg request scope
- `sqlalchemy_cow_session(engine_or_session, session_id=...)` — Equivalent optional SQLAlchemy async scope
- `CowPostgresConfig` — Dataclass for COW configuration
- `build_cow_variable_statements(session_id, operation_id)` — Build low-level transaction-local context statements

Low-level helpers require caller-managed connection, explicit transaction,
context validation, cancellation, and pool-cleanup safety. They are not the
recommended request integration.

## Development

```bash
git clone https://github.com/jpers1/agent-cow-python.git
cd agent-cow-python
pip install -e ".[dev]"
pytest agentcow/postgres/tests/ -v
```

## Contributing

For downstream questions, bug reports, or feature requests, use this fork's
[issue tracker](https://github.com/jpers1/agent-cow-python/issues).

## License

MIT License.

## Credits

Originally created by [Trail](https://trail-ml.com). This downstream fork is
maintained by `jpers1` while preserving upstream history and attribution.
