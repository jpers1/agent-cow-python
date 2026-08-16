"""Transaction-owning reviewer promotion and discard scopes.

The low-level promotion helpers accept an :class:`Executor` and therefore
cannot guarantee physical-connection or transaction lifetime.  This module
provides the stronger adapter-aware boundary used by hardened reviewer flows.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .core import (
    CowConflict,
    _get_cow_operation_tables,
    _lock_cow_session_tables,
    commit_cow_operations_schema,
    commit_cow_session_schema,
    discard_cow_operations_schema,
    discard_cow_session_schema,
    get_cow_conflicts,
    get_operation_dependencies,
    get_session_operations,
)
from .safe_session import (
    _AsyncpgAdapter,
    _SQLAlchemyAdapter,
    _describe_context,
    _normalize_context,
    _shield_cleanup,
)


class CowPromotionError(RuntimeError):
    """Base error for the high-level reviewer API."""


class CowPromotionStateError(CowPromotionError):
    """The reviewer scope cannot guarantee its connection or transaction."""


class CowPromotionRequestError(CowPromotionError, ValueError):
    """A requested session, operation selection, or policy is invalid."""


class CowConflictError(CowPromotionError):
    """Conflict-safe promotion rejected canonical-state divergence."""

    sqlstate = "40001"

    def __init__(
        self,
        session_id: uuid.UUID,
        schema: str,
        conflicts: tuple[CowConflict, ...] = (),
    ) -> None:
        self.session_id = session_id
        self.schema = schema
        self.conflicts = conflicts
        detail = f" ({len(conflicts)} structured conflict(s))" if conflicts else ""
        super().__init__(
            f"COW promotion conflict for session {session_id} in schema "
            f"{schema!r}{detail}"
        )


@dataclass(frozen=True)
class PromotionResult:
    """Structured outcome from a successful reviewer promotion."""

    session_id: uuid.UUID
    schema: str
    committed_tables: tuple[str, ...]
    committed_operations: tuple[uuid.UUID, ...]
    conflict_policy: str
    has_pending_operations: bool
    no_op: bool


@dataclass(frozen=True)
class DiscardResult:
    """Structured outcome from a successful reviewer discard."""

    session_id: uuid.UUID
    schema: str
    discarded_tables: tuple[str, ...]
    discarded_operations: tuple[uuid.UUID, ...]
    has_pending_operations: bool
    no_op: bool


def _as_uuid(value: str | uuid.UUID, name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CowPromotionRequestError(f"{name} must be a valid UUID") from exc


def _as_operation_ids(
    values: list[str | uuid.UUID] | tuple[str | uuid.UUID, ...],
) -> list[uuid.UUID]:
    return list(dict.fromkeys(_as_uuid(value, "operation ID") for value in values))


def _sqlstate(exc: BaseException) -> str | None:
    state = getattr(exc, "sqlstate", None)
    if state:
        return state
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def _is_cow_conflict(exc: BaseException) -> bool:
    """Distinguish H06's 40001 from unrelated serialization failures."""
    if _sqlstate(exc) != "40001":
        return False
    original = getattr(exc, "orig", None)
    return "COW conflict on" in str(original or exc)


class CowReviewer:
    """One active, transaction-owning reviewer scope.

    A scope permits conflict/dependency inspection followed by at most one
    promotion or discard action.  The adapter's native connection/session is
    exposed for trusted reviewer work, but direct SQL is an advanced escape
    hatch and is revalidated before any library-controlled mutation.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self._active = True
        self._terminal_action = False
        self._failed = False

    @property
    def native(self) -> Any:
        return self._adapter.native

    @property
    def is_active(self) -> bool:
        return self._active and self._adapter.in_transaction()

    def _require_active(self) -> None:
        if not self.is_active:
            raise CowPromotionStateError(
                "the reviewer promotion transaction is not active"
            )

    async def validate_context(self) -> None:
        """Require a clean runtime GUC context throughout reviewer work."""
        self._require_active()
        observed = _normalize_context(await self._adapter.get_context())
        if any(observed):
            raise CowPromotionStateError(
                "reviewer promotion requires clean PostgreSQL COW context; "
                f"observed {_describe_context(observed)}"
            )

    def _begin_terminal_action(self) -> None:
        self._require_active()
        if self._terminal_action:
            raise CowPromotionStateError(
                "one reviewer scope supports exactly one promotion or discard action"
            )
        self._terminal_action = True

    async def operations(
        self,
        session_id: str | uuid.UUID,
        *,
        schema: str = "public",
    ) -> list[uuid.UUID]:
        await self.validate_context()
        return await get_session_operations(
            self._adapter, _as_uuid(session_id, "session_id"), schema
        )

    async def dependencies(
        self,
        session_id: str | uuid.UUID,
        *,
        schema: str = "public",
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        await self.validate_context()
        return await get_operation_dependencies(
            self._adapter, _as_uuid(session_id, "session_id"), schema
        )

    async def conflicts(
        self,
        session_id: str | uuid.UUID,
        *,
        schema: str = "public",
        operation_ids: (
            list[str | uuid.UUID] | tuple[str | uuid.UUID, ...] | None
        ) = None,
    ) -> list[CowConflict]:
        await self.validate_context()
        selected = None if operation_ids is None else _as_operation_ids(operation_ids)
        return await get_cow_conflicts(
            self._adapter,
            _as_uuid(session_id, "session_id"),
            schema,
            selected,
        )

    async def _locked_state(
        self,
        session_id: uuid.UUID,
        schema: str,
        operation_ids: list[uuid.UUID] | None = None,
    ) -> tuple[list[str], list[uuid.UUID], list[CowConflict]]:
        tables = await _lock_cow_session_tables(self._adapter, session_id, schema)
        operations = await get_session_operations(self._adapter, session_id, schema)
        conflicts = await get_cow_conflicts(
            self._adapter, session_id, schema, operation_ids
        )
        return tables, operations, conflicts

    async def commit_session(
        self,
        session_id: str | uuid.UUID,
        *,
        schema: str = "public",
        defer_fk_constraints: bool = False,
        conflict_policy: str = "error",
    ) -> PromotionResult:
        """Promote a whole session atomically across its dirty tables."""
        selected_session = _as_uuid(session_id, "session_id")
        if conflict_policy not in {"error", "overwrite"}:
            raise CowPromotionRequestError(
                "conflict_policy must be 'error' or 'overwrite'"
            )
        await self.validate_context()
        self._begin_terminal_action()
        try:
            _tables, operations, conflicts = await self._locked_state(
                selected_session, schema
            )
            if conflict_policy == "error" and conflicts:
                raise CowConflictError(selected_session, schema, tuple(conflicts))
            committed = await commit_cow_session_schema(
                self._adapter,
                selected_session,
                schema,
                defer_fk_constraints,
                conflict_policy,
            )
            pending = await get_session_operations(
                self._adapter, selected_session, schema
            )
            return PromotionResult(
                session_id=selected_session,
                schema=schema,
                committed_tables=tuple(committed),
                committed_operations=tuple(operations),
                conflict_policy=conflict_policy,
                has_pending_operations=bool(pending),
                no_op=not operations,
            )
        except BaseException as exc:
            self._failed = True
            if isinstance(exc, CowPromotionError):
                raise
            if _is_cow_conflict(exc):
                raise CowConflictError(selected_session, schema) from exc
            if isinstance(exc, ValueError):
                raise CowPromotionRequestError(str(exc)) from exc
            raise

    async def discard_session(
        self,
        session_id: str | uuid.UUID,
        *,
        schema: str = "public",
    ) -> DiscardResult:
        """Discard a whole session atomically without touching canonical rows."""
        selected_session = _as_uuid(session_id, "session_id")
        await self.validate_context()
        self._begin_terminal_action()
        try:
            await _lock_cow_session_tables(self._adapter, selected_session, schema)
            operations = await get_session_operations(
                self._adapter, selected_session, schema
            )
            discarded = await discard_cow_session_schema(
                self._adapter, selected_session, schema
            )
            pending = await get_session_operations(
                self._adapter, selected_session, schema
            )
            return DiscardResult(
                session_id=selected_session,
                schema=schema,
                discarded_tables=tuple(discarded),
                discarded_operations=tuple(operations),
                has_pending_operations=bool(pending),
                no_op=not operations,
            )
        except BaseException as exc:
            self._failed = True
            if isinstance(exc, CowPromotionError):
                raise
            if isinstance(exc, ValueError):
                raise CowPromotionRequestError(str(exc)) from exc
            raise

    async def commit_operations(
        self,
        session_id: str | uuid.UUID,
        operation_ids: list[str | uuid.UUID] | tuple[str | uuid.UUID, ...],
        *,
        schema: str = "public",
        defer_fk_constraints: bool = False,
        conflict_policy: str = "error",
    ) -> PromotionResult:
        """Promote a causally valid operation set across all affected tables."""
        selected_session = _as_uuid(session_id, "session_id")
        requested = _as_operation_ids(operation_ids)
        if conflict_policy not in {"error", "overwrite"}:
            raise CowPromotionRequestError(
                "conflict_policy must be 'error' or 'overwrite'"
            )
        await self.validate_context()
        self._begin_terminal_action()
        try:
            await _lock_cow_session_tables(self._adapter, selected_session, schema)
            pending_before = await get_session_operations(
                self._adapter, selected_session, schema
            )
            requested_set = set(requested)
            selected = [op for op in pending_before if op in requested_set]
            if not selected:
                return PromotionResult(
                    session_id=selected_session,
                    schema=schema,
                    committed_tables=(),
                    committed_operations=(),
                    conflict_policy=conflict_policy,
                    has_pending_operations=bool(pending_before),
                    no_op=True,
                )
            conflicts = await get_cow_conflicts(
                self._adapter, selected_session, schema, selected
            )
            if conflict_policy == "error" and conflicts:
                raise CowConflictError(selected_session, schema, tuple(conflicts))
            committed = await commit_cow_operations_schema(
                self._adapter,
                selected_session,
                selected,
                schema,
                defer_fk_constraints,
                conflict_policy,
            )
            pending = await get_session_operations(
                self._adapter, selected_session, schema
            )
            return PromotionResult(
                session_id=selected_session,
                schema=schema,
                committed_tables=tuple(committed),
                committed_operations=tuple(selected),
                conflict_policy=conflict_policy,
                has_pending_operations=bool(pending),
                no_op=not selected,
            )
        except BaseException as exc:
            self._failed = True
            if isinstance(exc, CowPromotionError):
                raise
            if _is_cow_conflict(exc):
                raise CowConflictError(selected_session, schema) from exc
            if isinstance(exc, ValueError):
                raise CowPromotionRequestError(str(exc)) from exc
            raise

    async def discard_operations(
        self,
        session_id: str | uuid.UUID,
        operation_ids: list[str | uuid.UUID] | tuple[str | uuid.UUID, ...],
        *,
        schema: str = "public",
    ) -> DiscardResult:
        """Discard a causally valid operation set across affected tables."""
        selected_session = _as_uuid(session_id, "session_id")
        requested = _as_operation_ids(operation_ids)
        await self.validate_context()
        self._begin_terminal_action()
        try:
            await _lock_cow_session_tables(self._adapter, selected_session, schema)
            pending_before = await get_session_operations(
                self._adapter, selected_session, schema
            )
            requested_set = set(requested)
            selected = [op for op in pending_before if op in requested_set]
            discarded_tables = await _get_cow_operation_tables(
                self._adapter, selected_session, selected, schema
            )
            await discard_cow_operations_schema(
                self._adapter, selected_session, selected, schema
            )
            pending = await get_session_operations(
                self._adapter, selected_session, schema
            )
            return DiscardResult(
                session_id=selected_session,
                schema=schema,
                discarded_tables=tuple(discarded_tables),
                discarded_operations=tuple(selected),
                has_pending_operations=bool(pending),
                no_op=not selected,
            )
        except BaseException as exc:
            self._failed = True
            if isinstance(exc, CowPromotionError):
                raise
            if isinstance(exc, ValueError):
                raise CowPromotionRequestError(str(exc)) from exc
            raise


@asynccontextmanager
async def _managed_cow_reviewer(adapter: Any) -> AsyncIterator[CowReviewer]:
    if adapter.in_transaction():
        raise CowPromotionStateError(
            "safe reviewer scopes require a connection with no active transaction"
        )

    await adapter.begin()
    reviewer = CowReviewer(adapter)
    primary_error: BaseException | None = None
    try:
        await reviewer.validate_context()
        yield reviewer
        if reviewer._active and not adapter.in_transaction():
            raise CowPromotionStateError(
                "the owned reviewer transaction ended unexpectedly"
            )
        if reviewer._active:
            if reviewer._failed:
                await _shield_cleanup(adapter.rollback())
            else:
                await reviewer.validate_context()
                await adapter.commit()
            reviewer._active = False
    except BaseException as exc:
        primary_error = exc
        if adapter.in_transaction():
            await _shield_cleanup(adapter.rollback())
        reviewer._active = False
        raise
    finally:
        if adapter.in_transaction():
            await _shield_cleanup(adapter.rollback())
            reviewer._active = False
        try:
            leaked = _normalize_context(
                await _shield_cleanup(adapter.clean_after_transaction())
            )
        except BaseException:
            if primary_error is None:
                raise
        else:
            if any(leaked) and primary_error is None:
                raise CowPromotionStateError(
                    "COW context survived reviewer transaction cleanup and was "
                    f"reset: {_describe_context(leaked)}"
                )


@asynccontextmanager
async def asyncpg_cow_reviewer(connection_or_pool: Any) -> AsyncIterator[CowReviewer]:
    """Own one asyncpg connection and transaction for reviewer work."""
    is_pool = callable(getattr(connection_or_pool, "acquire", None)) and callable(
        getattr(connection_or_pool, "release", None)
    )
    connection = await connection_or_pool.acquire() if is_pool else connection_or_pool
    if not callable(getattr(connection, "transaction", None)) or not callable(
        getattr(connection, "fetch", None)
    ):
        if is_pool:
            await connection_or_pool.release(connection)
        raise TypeError("asyncpg_cow_reviewer requires asyncpg.Connection or Pool")
    try:
        async with _managed_cow_reviewer(_AsyncpgAdapter(connection)) as reviewer:
            yield reviewer
    finally:
        if is_pool:
            await _shield_cleanup(connection_or_pool.release(connection))


@asynccontextmanager
async def sqlalchemy_cow_reviewer(
    engine_connection_session_or_factory: Any,
) -> AsyncIterator[CowReviewer]:
    """Own one optional SQLAlchemy asyncio resource and transaction."""
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import (
            AsyncConnection,
            AsyncEngine,
            AsyncSession,
            async_sessionmaker,
        )
    except ImportError as exc:  # pragma: no cover - optional install
        raise RuntimeError(
            "sqlalchemy_cow_reviewer requires the 'sqlalchemy' optional dependency"
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
            "sqlalchemy_cow_reviewer requires AsyncEngine, AsyncConnection, "
            "AsyncSession, or async_sessionmaker"
        )

    try:
        async with _managed_cow_reviewer(_SQLAlchemyAdapter(native, text)) as reviewer:
            yield reviewer
    finally:
        if owned:
            await _shield_cleanup(native.close())
