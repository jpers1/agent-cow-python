"""Regression coverage for transaction-owning COW session adapters."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import registry

from agentcow.postgres import (
    CowSessionContextError,
    CowSessionStateError,
    asyncpg_cow_session,
    sqlalchemy_cow_session,
)

from conftest import PG_HOST, PG_PASSWORD, PG_PORT
from test_role_hardening import _hardened_environment


async def _pool(env, *, max_size: int = 1) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=PG_HOST,
        port=PG_PORT,
        user=env.runtime_role,
        password=PG_PASSWORD,
        database=env.setup.info.dbname,
        min_size=1,
        max_size=max_size,
    )


async def _context(connection) -> tuple[str | None, str | None, str | None]:
    row = await connection.fetchrow(
        "SELECT "
        "nullif(pg_catalog.current_setting('app.session_id', true), ''), "
        "nullif(pg_catalog.current_setting('app.operation_id', true), ''), "
        "nullif(pg_catalog.current_setting('app.visible_operations', true), '')"
    )
    return row[0], row[1], row[2]


@pytest.mark.asyncio
async def test_asyncpg_normal_commit_explicit_and_exception_rollback(postgresql):
    """Normal exit commits COW rows; both rollback paths remove them."""
    async with _hardened_environment(postgresql) as env:
        pool = await _pool(env)
        try:
            session_id = uuid.uuid4()
            async with asyncpg_cow_session(pool, session_id=session_id) as cow:
                assert cow.native.is_in_transaction() is True
                generated_operation = cow.operation_id
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".items VALUES (100, 'inserted')"
                )
                await cow.set_operation()
                await cow.execute(
                    f"UPDATE \"{env.schema}\".items SET value = 'updated' WHERE id = 1"
                )
                await cow.set_operation()
                await cow.execute(f'DELETE FROM "{env.schema}".items WHERE id = 2')
                assert await cow.execute(
                    f'SELECT id, value FROM "{env.schema}".items ORDER BY id'
                ) == [(1, "updated"), (3, "three"), (100, "inserted")]

            assert env.setup.execute(
                f'SELECT id, value FROM "{env.schema}".items_base ORDER BY id'
            ).fetchall() == [(1, "one"), (2, "two"), (3, "three")]
            assert env.setup.execute(
                f'SELECT operation_id FROM "{env.schema}".items_changes '
                f"WHERE session_id = '{session_id}'::uuid ORDER BY _cow_order"
            ).fetchall()[0] == (generated_operation,)

            rollback_session = uuid.uuid4()
            async with asyncpg_cow_session(pool, session_id=rollback_session) as cow:
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".items VALUES (200, 'rollback')"
                )
                await cow.rollback()
                with pytest.raises(CowSessionStateError):
                    await cow.execute("SELECT 1")

            exception_session = uuid.uuid4()
            with pytest.raises(RuntimeError, match="application failure"):
                async with asyncpg_cow_session(
                    pool, session_id=exception_session
                ) as cow:
                    await cow.execute(
                        f"INSERT INTO \"{env.schema}\".items VALUES (201, 'exception')"
                    )
                    raise RuntimeError("application failure")

            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".items_changes '
                f"WHERE session_id IN ('{rollback_session}'::uuid, "
                f"'{exception_session}'::uuid)"
            ).fetchone() == (0,)
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_asyncpg_pool_reuse_has_no_context_leak(postgresql):
    """A one-connection pool reuses the backend without reusing COW state."""
    async with _hardened_environment(postgresql) as env:
        pool = await _pool(env)
        try:
            session_a = uuid.uuid4()
            async with asyncpg_cow_session(pool, session_id=session_a) as cow:
                pid_a = (await cow.execute("SELECT pg_backend_pid()"))[0][0]
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".items VALUES (300, 'A')"
                )

            async with pool.acquire() as connection:
                assert await _context(connection) == (None, None, None)

            session_b = uuid.uuid4()
            async with asyncpg_cow_session(pool, session_id=session_b) as cow:
                pid_b = (await cow.execute("SELECT pg_backend_pid()"))[0][0]
                assert pid_b == pid_a
                assert await cow.execute(
                    f'SELECT count(*) FROM "{env.schema}".items WHERE id = 300'
                ) == [(0,)]
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".items VALUES (301, 'B')"
                )

            rows = env.setup.execute(
                f'SELECT session_id, value FROM "{env.schema}".items_changes '
                "WHERE id IN (300, 301) ORDER BY value"
            ).fetchall()
            assert rows == [(session_a, "A"), (session_b, "B")]
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_asyncpg_concurrent_pool_sessions_keep_separate_overlays(postgresql):
    """Concurrent acquired connections retain only their intended session."""
    async with _hardened_environment(postgresql) as env:
        pool = await _pool(env, max_size=2)
        ready = asyncio.Event()
        ready_count = 0
        ready_lock = asyncio.Lock()

        async def run(label: str, session_id: uuid.UUID) -> tuple[int, str]:
            nonlocal ready_count
            async with asyncpg_cow_session(pool, session_id=session_id) as cow:
                async with ready_lock:
                    ready_count += 1
                    if ready_count == 2:
                        ready.set()
                await ready.wait()
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".items VALUES (400, '{label}')"
                )
                rows = await cow.execute(
                    f'SELECT id, value FROM "{env.schema}".items WHERE id = 400'
                )
                observed_session = (
                    await cow.execute("SELECT current_setting('app.session_id', true)")
                )[0][0]
                assert observed_session == str(session_id)
                return rows[0]

        session_a = uuid.uuid4()
        session_b = uuid.uuid4()
        try:
            assert await asyncio.gather(run("A", session_a), run("B", session_b)) == [
                (400, "A"),
                (400, "B"),
            ]
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_asyncpg_cancellation_rolls_back_and_releases_clean_connection(
    postgresql,
):
    """Task cancellation cannot strand a transaction or pooled COW context."""
    async with _hardened_environment(postgresql) as env:
        pool = await _pool(env)
        entered = asyncio.Event()
        blocker = asyncio.Event()
        cancelled_session = uuid.uuid4()

        async def request() -> None:
            async with asyncpg_cow_session(pool, session_id=cancelled_session) as cow:
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".items VALUES (500, 'cancelled')"
                )
                entered.set()
                await blocker.wait()

        task = asyncio.create_task(request())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        try:
            async with pool.acquire() as connection:
                assert await _context(connection) == (None, None, None)
                assert connection.is_in_transaction() is False
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".items_changes '
                f"WHERE session_id = '{cancelled_session}'::uuid"
            ).fetchone() == (0,)

            async with asyncpg_cow_session(pool, session_id=uuid.uuid4()) as cow:
                assert await cow.execute("SELECT 1") == [(1,)]
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_context_input_generation_and_stale_detection(postgresql):
    """Inputs are validated early and stale physical-connection state is rejected."""
    async with _hardened_environment(postgresql) as env:
        pool = await _pool(env)
        try:
            with pytest.raises(ValueError, match="session_id is required"):
                async with asyncpg_cow_session(pool, session_id=None):
                    pass
            with pytest.raises(ValueError, match="session_id must be a valid UUID"):
                async with asyncpg_cow_session(pool, session_id="not-a-uuid"):
                    pass
            with pytest.raises(ValueError, match="operation_id must be a valid UUID"):
                async with asyncpg_cow_session(
                    pool, session_id=uuid.uuid4(), operation_id="bad"
                ):
                    pass

            async with asyncpg_cow_session(pool, session_id=uuid.uuid4()) as cow:
                assert isinstance(cow.operation_id, uuid.UUID)
                await cow.validate_context()
        finally:
            await pool.close()

        connection = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=env.runtime_role,
            password=PG_PASSWORD,
            database=env.setup.info.dbname,
        )
        try:
            await connection.execute(f"SET app.session_id = '{uuid.uuid4()}'")
            with pytest.raises(CowSessionContextError, match="stale"):
                async with asyncpg_cow_session(connection, session_id=uuid.uuid4()):
                    pass
            assert await _context(connection) == (None, None, None)

            async with asyncpg_cow_session(connection, session_id=uuid.uuid4()) as cow:
                assert await cow.execute("SELECT 1") == [(1,)]
            assert await _context(connection) == (None, None, None)

            transaction = connection.transaction()
            await transaction.start()
            try:
                with pytest.raises(CowSessionStateError, match="active transaction"):
                    async with asyncpg_cow_session(connection, session_id=uuid.uuid4()):
                        pass
            finally:
                await transaction.rollback()
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_operation_switching_visible_operations_and_h01_order(postgresql):
    """Explicit operation rotation preserves visibility and monotonic ordering."""
    async with _hardened_environment(postgresql) as env:
        pool = await _pool(env)
        session_id = uuid.uuid4()
        operation_a = uuid.uuid4()
        operation_b = uuid.uuid4()
        try:
            async with asyncpg_cow_session(
                pool,
                session_id=session_id,
                operation_id=operation_a,
            ) as cow:
                await cow.execute(
                    f'UPDATE "{env.schema}".items '
                    "SET value = 'operation A' WHERE id = 1"
                )
                assert await cow.set_operation(operation_b) == operation_b
                await cow.execute(
                    f'UPDATE "{env.schema}".items '
                    "SET value = 'operation B' WHERE id = 1"
                )

                await cow.set_visible_operations([operation_a])
                assert await cow.execute(
                    f'SELECT value FROM "{env.schema}".items WHERE id = 1'
                ) == [("operation A",)]
                await cow.set_visible_operations([operation_a, operation_b])
                assert await cow.execute(
                    f'SELECT value FROM "{env.schema}".items WHERE id = 1'
                ) == [("operation B",)]
                await cow.set_visible_operations(None)
                assert await cow.execute(
                    f'SELECT value FROM "{env.schema}".items WHERE id = 1'
                ) == [("operation B",)]

            changes = env.setup.execute(
                f"SELECT operation_id, _cow_order "
                f'FROM "{env.schema}".items_changes '
                f"WHERE session_id = '{session_id}'::uuid ORDER BY _cow_order"
            ).fetchall()
            assert [row[0] for row in changes] == [operation_a, operation_b]
            assert changes[0][1] < changes[1][1]
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_reset_context_fails_closed_without_canonical_write(postgresql):
    """Raw context mutation is detected and H03 still blocks the write itself."""
    async with _hardened_environment(postgresql) as env:
        pool = await _pool(env)
        try:
            with pytest.raises(asyncpg.exceptions.InvalidParameterValueError):
                async with asyncpg_cow_session(pool, session_id=uuid.uuid4()) as cow:
                    await cow.native.execute("RESET app.session_id")
                    await cow.native.execute(
                        f"INSERT INTO \"{env.schema}\".items VALUES (600, 'blocked')"
                    )

            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".items_base WHERE id = 600'
            ).fetchone() == (0,)

            with pytest.raises(CowSessionContextError, match="changed"):
                async with asyncpg_cow_session(pool, session_id=uuid.uuid4()) as cow:
                    await cow.native.execute("RESET app.operation_id")
                    await cow.execute("SELECT 1")

            with pytest.raises(CowSessionStateError, match="ended unexpectedly"):
                async with asyncpg_cow_session(pool, session_id=uuid.uuid4()) as cow:
                    await cow.native.execute("COMMIT")

            leaked_session = uuid.uuid4()
            with pytest.raises(CowSessionContextError, match="survived"):
                async with asyncpg_cow_session(pool, session_id=leaked_session) as cow:
                    # A non-LOCAL SET to the expected value passes the active
                    # value check but would survive COMMIT without cleanup.
                    await cow.native.execute(f"SET app.session_id = '{leaked_session}'")
            async with pool.acquire() as connection:
                assert await _context(connection) == (None, None, None)
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_sqlalchemy_engine_and_sessionmaker_own_safe_transactions(postgresql):
    """Both SQLAlchemy async entry points commit, roll back, and clear context."""
    async with _hardened_environment(postgresql) as env:
        url = URL.create(
            "postgresql+asyncpg",
            username=env.runtime_role,
            password=PG_PASSWORD,
            host=PG_HOST,
            port=PG_PORT,
            database=env.setup.info.dbname,
        )
        engine = create_async_engine(url, pool_size=1, max_overflow=0)
        # Disabling SQLAlchemy autobegin proves the scope owns both the main
        # request transaction and its post-transaction cleanliness check.
        maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autobegin=False,
        )
        mapper_registry = registry()
        items = Table(
            "items",
            MetaData(),
            Column("id", Integer, primary_key=True),
            Column("value", String, nullable=False),
            schema=env.schema,
        )

        class Item:
            pass

        mapper_registry.map_imperatively(Item, items)
        try:
            session_a = uuid.uuid4()
            async with sqlalchemy_cow_session(engine, session_id=session_a) as cow:
                assert cow.native.in_transaction() is True
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".items VALUES (700, 'engine')"
                )

            session_b = uuid.uuid4()
            async with sqlalchemy_cow_session(maker, session_id=session_b) as cow:
                assert await cow.execute(
                    f'SELECT count(*) FROM "{env.schema}".items WHERE id = 700'
                ) == [(0,)]
                cow.native.add(Item(id=701, value="session"))

            failed_session = uuid.uuid4()
            with pytest.raises(RuntimeError, match="rollback SQLAlchemy"):
                async with sqlalchemy_cow_session(
                    maker, session_id=failed_session
                ) as cow:
                    await cow.execute(
                        f"INSERT INTO \"{env.schema}\".items VALUES (702, 'rollback')"
                    )
                    raise RuntimeError("rollback SQLAlchemy")

            async with engine.connect() as connection:
                result = await connection.exec_driver_sql(
                    "SELECT "
                    "nullif(current_setting('app.session_id', true), ''), "
                    "nullif(current_setting('app.operation_id', true), ''), "
                    "nullif(current_setting('app.visible_operations', true), '')"
                )
                assert result.one() == (None, None, None)
                await connection.rollback()

            assert env.setup.execute(
                f'SELECT session_id, value FROM "{env.schema}".items_changes '
                "WHERE id IN (700, 701) ORDER BY id"
            ).fetchall() == [(session_a, "engine"), (session_b, "session")]
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".items_changes '
                f"WHERE session_id = '{failed_session}'::uuid"
            ).fetchone() == (0,)
        finally:
            await engine.dispose()
