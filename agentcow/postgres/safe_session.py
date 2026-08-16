"""Driver-aware, transaction-owning PostgreSQL COW session scopes.

The low-level :class:`~agentcow.postgres.core.Executor` protocol intentionally
does not describe connection or transaction lifetime.  This module provides
the stronger adapter-specific boundary needed by request handlers: one
physical connection, one explicit transaction, and transaction-local COW
context that is checked against PostgreSQL state.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol

_CONTEXT_NAMES = (
    "app.session_id",
    "app.operation_id",
    "app.visible_operations",
)


class CowSessionError(RuntimeError):
    """Base error raised by the safe COW session API."""


class CowSessionStateError(CowSessionError):
    """The adapter cannot establish or continue the required transaction."""


class CowSessionContextError(CowSessionError):
    """PostgreSQL COW context is stale, missing, or unexpectedly changed."""


class _SessionAdapter(Protocol):
    native: Any

    def in_transaction(self) -> bool: ...

    async def begin(self) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def execute(self, sql: str) -> list[tuple[Any, ...]]: ...

    async def get_context(self) -> tuple[str | None, str | None, str | None]: ...

    async def set_context_value(self, name: str, value: str) -> None: ...

    async def clean_after_transaction(
        self,
    ) -> tuple[str | None, str | None, str | None]: ...


def _as_uuid(value: str | uuid.UUID | None, name: str) -> uuid.UUID:
    if value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a valid UUID") from exc


def _as_visible_operations(
    values: list[str | uuid.UUID] | tuple[str | uuid.UUID, ...] | None,
) -> tuple[uuid.UUID, ...] | None:
    if values is None:
        return None
    return tuple(_as_uuid(value, "visible operation ID") for value in values)


def _normalize_context(
    values: tuple[str | None, str | None, str | None],
) -> tuple[str | None, str | None, str | None]:
    return values[0] or None, values[1] or None, values[2] or None


def _visible_value(values: tuple[uuid.UUID, ...] | None) -> str:
    if not values:
        return ""
    return ",".join(str(value) for value in values)


def _expected_context(
    session_id: uuid.UUID,
    operation_id: uuid.UUID,
    visible_operations: tuple[uuid.UUID, ...] | None,
) -> tuple[str | None, str | None, str | None]:
    return (
        str(session_id),
        str(operation_id),
        _visible_value(visible_operations) or None,
    )


def _describe_context(values: tuple[str | None, str | None, str | None]) -> str:
    return ", ".join(f"{name}={value!r}" for name, value in zip(_CONTEXT_NAMES, values))


async def _shield_cleanup(awaitable: Any) -> Any:
    """Let transaction cleanup finish even while the caller is cancelled."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        finally:
            raise


class CowSession:
    """An active, validated COW transaction.

    Use instances only inside :func:`asyncpg_cow_session` or
    :func:`sqlalchemy_cow_session`.  ``execute()`` validates the expected
    context before each statement.  ``native`` is the acquired driver object
    for adapter-specific or ORM work; direct use is a trusted escape hatch and
    cannot be protected from deliberately hostile SQL.
    """

    def __init__(
        self,
        adapter: _SessionAdapter,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
        visible_operations: tuple[uuid.UUID, ...] | None,
    ) -> None:
        self._adapter = adapter
        self.session_id = session_id
        self.operation_id = operation_id
        self.visible_operations = visible_operations
        self._active = True

    @property
    def native(self) -> Any:
        """The acquired asyncpg connection or SQLAlchemy async object."""
        return self._adapter.native

    @property
    def is_active(self) -> bool:
        """Whether the owned transaction is still active."""
        return self._active and self._adapter.in_transaction()

    def _require_active(self) -> None:
        if not self.is_active:
            raise CowSessionStateError("the COW session transaction is not active")

    def _expected(self) -> tuple[str | None, str | None, str | None]:
        return _expected_context(
            self.session_id,
            self.operation_id,
            self.visible_operations,
        )

    async def validate_context(self) -> None:
        """Raise if PostgreSQL no longer has the intended local context."""
        self._require_active()
        observed = _normalize_context(await self._adapter.get_context())
        expected = self._expected()
        if observed != expected:
            raise CowSessionContextError(
                "COW context changed inside the active transaction; expected "
                f"{_describe_context(expected)}, observed "
                f"{_describe_context(observed)}"
            )

    async def execute(self, sql: str) -> list[tuple[Any, ...]]:
        """Validate context, then execute raw SQL on the owned connection."""
        await self.validate_context()
        return await self._adapter.execute(sql)

    async def set_operation(
        self,
        operation_id: str | uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Select the next trusted operation ID, generating one when omitted."""
        await self.validate_context()
        selected = (
            uuid.uuid4()
            if operation_id is None
            else _as_uuid(operation_id, "operation_id")
        )
        await self._adapter.set_context_value("app.operation_id", str(selected))
        self.operation_id = selected
        await self.validate_context()
        return selected

    async def set_visible_operations(
        self,
        operation_ids: list[str | uuid.UUID] | tuple[str | uuid.UUID, ...] | None,
    ) -> None:
        """Set and verify the operation subset visible to overlay reads."""
        await self.validate_context()
        selected = _as_visible_operations(operation_ids)
        await self._adapter.set_context_value(
            "app.visible_operations", _visible_value(selected)
        )
        self.visible_operations = selected
        await self.validate_context()

    async def rollback(self) -> None:
        """Explicitly roll back and close this scope's transaction."""
        self._require_active()
        await _shield_cleanup(self._adapter.rollback())
        self._active = False


@asynccontextmanager
async def _managed_cow_session(
    adapter: _SessionAdapter,
    *,
    session_id: str | uuid.UUID,
    operation_id: str | uuid.UUID | None,
    visible_operations: list[str | uuid.UUID] | tuple[str | uuid.UUID, ...] | None,
) -> AsyncIterator[CowSession]:
    selected_session = _as_uuid(session_id, "session_id")
    selected_operation = (
        uuid.uuid4() if operation_id is None else _as_uuid(operation_id, "operation_id")
    )
    selected_visible = _as_visible_operations(visible_operations)

    if adapter.in_transaction():
        raise CowSessionStateError(
            "safe COW sessions require a connection with no active transaction"
        )

    await adapter.begin()
    session = CowSession(
        adapter,
        selected_session,
        selected_operation,
        selected_visible,
    )
    primary_error: BaseException | None = None
    try:
        stale = _normalize_context(await adapter.get_context())
        if any(stale):
            raise CowSessionContextError(
                "refusing to overwrite stale PostgreSQL COW context: "
                f"{_describe_context(stale)}"
            )

        await adapter.set_context_value("app.session_id", str(selected_session))
        await adapter.set_context_value("app.operation_id", str(selected_operation))
        await adapter.set_context_value(
            "app.visible_operations", _visible_value(selected_visible)
        )
        await session.validate_context()

        yield session

        if session._active and not adapter.in_transaction():
            raise CowSessionStateError(
                "the owned transaction ended unexpectedly inside the COW scope"
            )
        if session._active:
            await session.validate_context()
            await adapter.commit()
            session._active = False
    except BaseException as exc:
        primary_error = exc
        if adapter.in_transaction():
            await _shield_cleanup(adapter.rollback())
        session._active = False
        raise
    finally:
        if adapter.in_transaction():
            await _shield_cleanup(adapter.rollback())
            session._active = False
        try:
            leaked = _normalize_context(
                await _shield_cleanup(adapter.clean_after_transaction())
            )
        except BaseException:
            if primary_error is None:
                raise
        else:
            if any(leaked) and primary_error is None:
                raise CowSessionContextError(
                    "COW context survived transaction cleanup and was reset: "
                    f"{_describe_context(leaked)}"
                )


class _AsyncpgAdapter:
    def __init__(self, connection: Any) -> None:
        self.native = connection
        self._transaction: Any = None

    def in_transaction(self) -> bool:
        return bool(self.native.is_in_transaction())

    async def begin(self) -> None:
        self._transaction = self.native.transaction()
        await self._transaction.start()

    async def commit(self) -> None:
        await self._transaction.commit()

    async def rollback(self) -> None:
        await self._transaction.rollback()

    async def execute(self, sql: str) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in await self.native.fetch(sql)]

    async def get_context(self) -> tuple[str | None, str | None, str | None]:
        row = await self.native.fetchrow(
            "SELECT "
            "pg_catalog.current_setting('app.session_id', true), "
            "pg_catalog.current_setting('app.operation_id', true), "
            "pg_catalog.current_setting('app.visible_operations', true)"
        )
        return row[0], row[1], row[2]

    async def set_context_value(self, name: str, value: str) -> None:
        await self.native.fetchval(
            "SELECT pg_catalog.set_config($1, $2, true)", name, value
        )

    async def clean_after_transaction(
        self,
    ) -> tuple[str | None, str | None, str | None]:
        observed = await self.get_context()
        if any(_normalize_context(observed)):
            for name in _CONTEXT_NAMES:
                await self.native.execute(f"RESET {name}")
        return observed


@asynccontextmanager
async def asyncpg_cow_session(
    connection_or_pool: Any,
    *,
    session_id: str | uuid.UUID,
    operation_id: str | uuid.UUID | None = None,
    visible_operations: (
        list[str | uuid.UUID] | tuple[str | uuid.UUID, ...] | None
    ) = None,
) -> AsyncIterator[CowSession]:
    """Own a safe COW transaction on an asyncpg connection or pool.

    A pool connection is acquired before the transaction and released only
    after transaction-local context has been checked and cleared.  Passing an
    already-transactional connection is rejected rather than creating a
    savepoint with weaker lifetime guarantees.
    """
    is_pool = callable(getattr(connection_or_pool, "acquire", None)) and callable(
        getattr(connection_or_pool, "release", None)
    )
    connection = await connection_or_pool.acquire() if is_pool else connection_or_pool
    if not callable(getattr(connection, "transaction", None)) or not callable(
        getattr(connection, "fetch", None)
    ):
        if is_pool:
            await connection_or_pool.release(connection)
        raise TypeError("asyncpg_cow_session requires asyncpg.Connection or Pool")

    try:
        async with _managed_cow_session(
            _AsyncpgAdapter(connection),
            session_id=session_id,
            operation_id=operation_id,
            visible_operations=visible_operations,
        ) as session:
            yield session
    finally:
        if is_pool:
            await _shield_cleanup(connection_or_pool.release(connection))


class _SQLAlchemyAdapter:
    def __init__(self, native: Any, text: Any) -> None:
        self.native = native
        self._text = text
        self._transaction: Any = None

    def in_transaction(self) -> bool:
        return bool(self.native.in_transaction())

    async def begin(self) -> None:
        self._transaction = await self.native.begin()

    async def commit(self) -> None:
        await self._transaction.commit()

    async def rollback(self) -> None:
        await self._transaction.rollback()

    async def execute(self, sql: str) -> list[tuple[Any, ...]]:
        result = await self.native.execute(self._text(sql))
        if result.returns_rows:
            return [tuple(row) for row in result.fetchall()]
        return []

    async def get_context(self) -> tuple[str | None, str | None, str | None]:
        result = await self.native.execute(
            self._text(
                "SELECT "
                "pg_catalog.current_setting('app.session_id', true), "
                "pg_catalog.current_setting('app.operation_id', true), "
                "pg_catalog.current_setting('app.visible_operations', true)"
            )
        )
        row = result.one()
        return row[0], row[1], row[2]

    async def set_context_value(self, name: str, value: str) -> None:
        await self.native.execute(
            self._text("SELECT pg_catalog.set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )

    async def clean_after_transaction(
        self,
    ) -> tuple[str | None, str | None, str | None]:
        cleanup_transaction = await self.native.begin()
        try:
            observed = await self.get_context()
            if any(_normalize_context(observed)):
                for name in _CONTEXT_NAMES:
                    await self.native.execute(self._text(f"RESET {name}"))
                await cleanup_transaction.commit()
            else:
                await cleanup_transaction.rollback()
            return observed
        except BaseException:
            if cleanup_transaction.is_active:
                await cleanup_transaction.rollback()
            raise


@asynccontextmanager
async def sqlalchemy_cow_session(
    engine_connection_session_or_factory: Any,
    *,
    session_id: str | uuid.UUID,
    operation_id: str | uuid.UUID | None = None,
    visible_operations: (
        list[str | uuid.UUID] | tuple[str | uuid.UUID, ...] | None
    ) = None,
) -> AsyncIterator[CowSession]:
    """Own a safe COW transaction using SQLAlchemy's asyncio integration.

    Supported inputs are ``AsyncEngine``, ``AsyncConnection``,
    ``AsyncSession``, and ``async_sessionmaker``.  The yielded
    :class:`CowSession` exposes the acquired SQLAlchemy object as ``native``
    for ORM use.  Objects opened by this function are closed after cleanup;
    caller-supplied connections and sessions remain open and transaction-free.
    """
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import (
            AsyncConnection,
            AsyncEngine,
            AsyncSession,
            async_sessionmaker,
        )
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "sqlalchemy_cow_session requires the 'sqlalchemy' optional dependency"
        ) from exc

    resource = engine_connection_session_or_factory
    owned = False
    if isinstance(resource, AsyncEngine):
        native = await resource.connect()
        owned = True
    elif isinstance(resource, async_sessionmaker):
        native = resource()
        owned = True
    elif isinstance(resource, (AsyncConnection, AsyncSession)):
        native = resource
    else:
        raise TypeError(
            "sqlalchemy_cow_session requires AsyncEngine, AsyncConnection, "
            "AsyncSession, or async_sessionmaker"
        )

    try:
        async with _managed_cow_session(
            _SQLAlchemyAdapter(native, text),
            session_id=session_id,
            operation_id=operation_id,
            visible_operations=visible_operations,
        ) as session:
            yield session
    finally:
        if owned:
            await _shield_cleanup(native.close())
