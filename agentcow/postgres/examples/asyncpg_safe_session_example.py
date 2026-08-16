"""Recommended asyncpg integration for an agent-cow-postgresql deployment.

The application, not an external caller, resolves an opaque credential to a
server-owned session UUID.  Runtime traffic uses only the hardened runtime
role and :func:`asyncpg_cow_session`; promotion uses a separate reviewer role.

``asyncpg`` is imported only by functions that open database resources, so
this module can be imported and inspected without an installed driver or a
running external service.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from agentcow.postgres import (
    CowConflictError,
    asyncpg_cow_reviewer,
    asyncpg_cow_session,
    deploy_cow_functions,
    enable_cow_schema,
    harden_cow_schema,
    validate_cow_schema_privileges,
)

APPLICATION_SCHEMA = "content"
RUNTIME_ROLE = "application_runtime"
REVIEWER_ROLE = "application_reviewer"


class AsyncpgExecutor:
    """Adapt one caller-owned asyncpg connection to the low-level API."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def execute(self, sql: str) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in await self._connection.fetch(sql)]


class ApplicationSessionStore(Protocol):
    """Application authorization boundary, deliberately outside the library."""

    async def resolve(self, external_capability: str) -> uuid.UUID:
        """Return a server-owned UUID after authenticating the capability."""
        ...


class ExampleRequestError(RuntimeError):
    """Application-level failure used only to demonstrate rollback."""


async def configure_hardened_schema(setup_dsn: str) -> None:
    """Deploy, enable, harden, and validate using the non-runtime owner role.

    The setup DSN must identify the ordinary role that owns the application
    schema and its tables.  Initial database and role creation is a separate
    PostgreSQL administration task; the setup role need not be a superuser.
    """
    import asyncpg

    connection = await asyncpg.connect(setup_dsn)
    try:
        async with connection.transaction():
            executor = AsyncpgExecutor(connection)
            await deploy_cow_functions(executor)
            await enable_cow_schema(
                executor,
                schema=APPLICATION_SCHEMA,
                exclude={"alembic_version"},
            )
            await harden_cow_schema(
                executor,
                schema=APPLICATION_SCHEMA,
                runtime_roles=[RUNTIME_ROLE],
                reviewer_roles=[REVIEWER_ROLE],
            )
            validation = await validate_cow_schema_privileges(
                executor,
                schema=APPLICATION_SCHEMA,
                runtime_roles=[RUNTIME_ROLE],
                reviewer_roles=[REVIEWER_ROLE],
            )
            if not validation["safe"]:
                raise RuntimeError(
                    "unsafe PostgreSQL role configuration: "
                    + "; ".join(validation["violations"])
                )
    finally:
        await connection.close()


async def create_runtime_pool(runtime_dsn: str) -> Any:
    """Create the preferred pool using only hardened runtime credentials."""
    import asyncpg

    return await asyncpg.create_pool(runtime_dsn, min_size=1, max_size=10)


async def run_authorized_request(
    runtime_pool: Any,
    application_session_store: ApplicationSessionStore,
    external_capability: str,
) -> tuple[tuple[Any, ...], ...]:
    """Run controlled CRUD after server-side authorization and UUID lookup.

    The external capability is never used as PostgreSQL context.  Any error or
    cancellation leaving the scope rolls back the entire request transaction.
    """
    trusted_session_id = await application_session_store.resolve(external_capability)

    async with asyncpg_cow_session(
        runtime_pool,
        session_id=trusted_session_id,
    ) as cow:
        await cow.execute(
            "INSERT INTO content.pages (id, title) VALUES (1001, 'Draft')"
        )
        await cow.set_operation()
        await cow.execute(
            "UPDATE content.pages SET title = 'Reviewed draft' WHERE id = 1001"
        )
        await cow.set_operation()
        await cow.execute("DELETE FROM content.pages WHERE id = 1002")
        return tuple(
            await cow.execute("SELECT id, title FROM content.pages ORDER BY id")
        )


async def demonstrate_error_rollback(
    runtime_pool: Any, trusted_session_id: uuid.UUID
) -> None:
    """Show that an application error rolls back the owned request transaction."""
    try:
        async with asyncpg_cow_session(
            runtime_pool, session_id=trusted_session_id
        ) as cow:
            await cow.execute(
                "INSERT INTO content.pages (id, title) VALUES (1003, 'temporary')"
            )
            raise ExampleRequestError("application request failed")
    except ExampleRequestError:
        pass


async def review_and_promote(
    reviewer_pool: Any,
    trusted_session_id: uuid.UUID,
    *,
    approve: bool,
) -> tuple[list[uuid.UUID], list[tuple[uuid.UUID, uuid.UUID]]]:
    """Inspect and then commit or discard using only the reviewer role.

    Authorization for *approve* belongs to the application or human review
    workflow. Conflict inspection supports review, while the commit itself
    independently enforces the first-touch baseline under a database lock.
    """
    async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
        operations = await reviewer.operations(
            trusted_session_id, schema=APPLICATION_SCHEMA
        )
        dependencies = await reviewer.dependencies(
            trusted_session_id, schema=APPLICATION_SCHEMA
        )
        if approve:
            try:
                await reviewer.commit_session(
                    trusted_session_id, schema=APPLICATION_SCHEMA
                )
            except CowConflictError as exc:
                raise ExampleRequestError(
                    f"promotion has {len(exc.conflicts)} canonical conflict(s)"
                ) from exc
        else:
            await reviewer.discard_session(
                trusted_session_id, schema=APPLICATION_SCHEMA
            )
        return operations, dependencies
