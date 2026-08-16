"""Regression coverage for atomic, transaction-owning reviewer promotion."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from agentcow.postgres import (
    CowConflictError,
    CowPromotionRequestError,
    CowPromotionStateError,
    asyncpg_cow_reviewer,
    asyncpg_cow_session,
    enable_cow,
    harden_cow_schema,
    sqlalchemy_cow_reviewer,
)

from conftest import PG_HOST, PG_PASSWORD, PG_PORT
from test_role_hardening import _hardened_environment


async def _pool(env, role: str, *, max_size: int = 2) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=PG_HOST,
        port=PG_PORT,
        user=role,
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


async def _enable_tables(env, definitions: list[str], names: list[str]) -> None:
    for definition in definitions:
        env.setup.execute(definition)
    for name in names:
        await enable_cow(env.setup_executor, name, schema=env.schema)
    validation = await harden_cow_schema(
        env.setup_executor,
        schema=env.schema,
        runtime_roles=[env.runtime_role],
        reviewer_roles=[env.reviewer_role],
    )
    assert validation == {"safe": True, "violations": []}
    env.setup.commit()


async def _record_item_update(env, session_id: uuid.UUID, value: str) -> uuid.UUID:
    runtime_pool = await _pool(env, env.runtime_role, max_size=1)
    try:
        operation_id = uuid.uuid4()
        async with asyncpg_cow_session(
            runtime_pool, session_id=session_id, operation_id=operation_id
        ) as cow:
            await cow.execute(
                f"UPDATE \"{env.schema}\".items SET value = '{value}' WHERE id = 1"
            )
        return operation_id
    finally:
        await runtime_pool.close()


@pytest.mark.asyncio
async def test_full_multitable_commit_owns_connection_transaction_and_cleans_pool(
    postgresql,
):
    """Related-table promotion is one non-public-schema transaction."""
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".review_parents '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".review_children '
                "(id integer PRIMARY KEY, parent_id integer NOT NULL "
                f'REFERENCES "{env.schema}".review_parents(id), '
                "value text NOT NULL)",
            ],
            ["review_parents", "review_children"],
        )
        runtime_pool = await _pool(env, env.runtime_role)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            parent_operation = uuid.uuid4()
            child_operation = uuid.uuid4()
            async with asyncpg_cow_session(
                runtime_pool,
                session_id=session_id,
                operation_id=parent_operation,
            ) as cow:
                await cow.execute(
                    f'INSERT INTO "{env.schema}".review_parents '
                    "VALUES (10, 'parent')"
                )
                await cow.set_operation(child_operation)
                await cow.execute(
                    f'INSERT INTO "{env.schema}".review_children '
                    "VALUES (20, 10, 'child')"
                )

            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                assert reviewer.native.is_in_transaction()
                assert await reviewer.operations(session_id, schema=env.schema) == [
                    parent_operation,
                    child_operation,
                ]
                assert await reviewer.conflicts(session_id, schema=env.schema) == []
                result = await reviewer.commit_session(session_id, schema=env.schema)

            assert result.committed_tables == (
                "review_parents",
                "review_children",
            )
            assert result.committed_operations == (
                parent_operation,
                child_operation,
            )
            assert result.has_pending_operations is False
            assert result.no_op is False
            assert env.setup.execute(
                f'SELECT id, value FROM "{env.schema}".review_parents_base'
            ).fetchall() == [(10, "parent")]
            assert env.setup.execute(
                f"SELECT id, parent_id, value FROM "
                f'"{env.schema}".review_children_base'
            ).fetchall() == [(20, 10, "child")]

            async with reviewer_pool.acquire() as connection:
                assert connection.is_in_transaction() is False
                assert await _context(connection) == (None, None, None)
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_full_discard_and_terminal_idempotency_are_predictable(postgresql):
    """Duplicate/crossed terminal requests become structured no-ops."""
    async with _hardened_environment(postgresql) as env:
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            discarded_session = uuid.uuid4()
            discarded_operation = await _record_item_update(
                env, discarded_session, "discarded"
            )
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                discarded = await reviewer.discard_session(
                    discarded_session, schema=env.schema
                )
            assert discarded.discarded_tables == ("items",)
            assert discarded.discarded_operations == (discarded_operation,)
            assert discarded.no_op is False

            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                duplicate_discard = await reviewer.discard_session(
                    discarded_session, schema=env.schema
                )
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                commit_after_discard = await reviewer.commit_session(
                    discarded_session, schema=env.schema
                )
            assert duplicate_discard.no_op is True
            assert commit_after_discard.no_op is True

            committed_session = uuid.uuid4()
            await _record_item_update(env, committed_session, "committed")
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                committed = await reviewer.commit_session(
                    committed_session, schema=env.schema
                )
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                duplicate_commit = await reviewer.commit_session(
                    committed_session, schema=env.schema
                )
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                discard_after_commit = await reviewer.discard_session(
                    committed_session, schema=env.schema
                )
            assert committed.no_op is False
            assert duplicate_commit.no_op is True
            assert discard_after_commit.no_op is True
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("committed",)

            unknown = uuid.uuid4()
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                unknown_result = await reviewer.commit_session(
                    unknown, schema=env.schema
                )
            assert unknown_result.no_op is True
        finally:
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_multitable_discard_failure_rolls_back_then_succeeds(postgresql):
    """A later changes-table failure restores earlier discarded rows."""
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".alpha '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".beta '
                "(id integer PRIMARY KEY, value text NOT NULL)",
            ],
            ["alpha", "beta"],
        )
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            async with asyncpg_cow_session(runtime_pool, session_id=session_id) as cow:
                await cow.execute(f"INSERT INTO \"{env.schema}\".alpha VALUES (1, 'A')")
                await cow.set_operation()
                await cow.execute(f"INSERT INTO \"{env.schema}\".beta VALUES (1, 'B')")
            env.setup.execute(
                f'CREATE FUNCTION "{env.schema}".fail_discard() RETURNS trigger '
                "LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'discard failure'; END $$"
            )
            env.setup.execute(
                f"CREATE TRIGGER fail_discard BEFORE DELETE ON "
                f'"{env.schema}".beta_changes FOR EACH ROW EXECUTE FUNCTION '
                f'"{env.schema}".fail_discard()'
            )
            env.setup.commit()

            with pytest.raises(asyncpg.RaiseError, match="discard failure"):
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.discard_session(session_id, schema=env.schema)
            for table in ("alpha", "beta"):
                assert env.setup.execute(
                    f'SELECT count(*) FROM "{env.schema}".{table}_changes '
                    f"WHERE session_id = '{session_id}'::uuid"
                ).fetchone() == (1,)

            env.setup.execute(
                f'DROP TRIGGER fail_discard ON "{env.schema}".beta_changes'
            )
            env.setup.commit()
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                result = await reviewer.discard_session(session_id, schema=env.schema)
            assert result.discarded_tables == ("alpha", "beta")
            assert result.has_pending_operations is False
            assert (
                env.setup.execute(f'SELECT * FROM "{env.schema}".alpha_base').fetchall()
                == []
            )
            assert (
                env.setup.execute(f'SELECT * FROM "{env.schema}".beta_base').fetchall()
                == []
            )
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


async def _prepare_failure_case(env, mode: str) -> uuid.UUID:
    definitions = [
        f'CREATE TABLE "{env.schema}".a_good '
        "(id integer PRIMARY KEY, value text NOT NULL)",
        f'CREATE TABLE "{env.schema}".z_bad '
        "(id integer PRIMARY KEY, value integer CHECK (value > 0), "
        "code text UNIQUE)",
    ]
    await _enable_tables(env, definitions, ["a_good", "z_bad"])
    env.setup.execute(
        f"INSERT INTO \"{env.schema}\".z_bad_base VALUES (1, 1, 'existing')"
    )
    env.setup.commit()

    if mode in {"delete_phase", "cleanup"}:
        target = "z_bad_base" if mode == "delete_phase" else "a_good_changes"
        event = "DELETE"
        env.setup.execute(
            f'CREATE FUNCTION "{env.schema}".fail_{mode}() RETURNS trigger '
            "LANGUAGE plpgsql AS $$ BEGIN "
            f"RAISE EXCEPTION '{mode} failure'; END $$"
        )
        env.setup.execute(
            f"CREATE TRIGGER fail_{mode} BEFORE {event} ON "
            f'"{env.schema}".{target} FOR EACH ROW EXECUTE FUNCTION '
            f'"{env.schema}".fail_{mode}()'
        )
        env.setup.commit()

    runtime_pool = await _pool(env, env.runtime_role, max_size=1)
    try:
        session_id = uuid.uuid4()
        async with asyncpg_cow_session(runtime_pool, session_id=session_id) as cow:
            await cow.execute(
                f"INSERT INTO \"{env.schema}\".a_good VALUES (10, 'good')"
            )
            await cow.set_operation()
            if mode == "check":
                await cow.execute(
                    f'INSERT INTO "{env.schema}".z_bad ' "VALUES (20, -1, 'check')"
                )
            elif mode == "unique":
                await cow.execute(
                    f'INSERT INTO "{env.schema}".z_bad ' "VALUES (20, 2, 'existing')"
                )
            elif mode == "delete_phase":
                await cow.execute(f'DELETE FROM "{env.schema}".z_bad WHERE id = 1')
            else:
                await cow.execute(
                    f'INSERT INTO "{env.schema}".z_bad ' "VALUES (20, 2, 'cleanup')"
                )
        return session_id
    finally:
        await runtime_pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["check", "unique", "delete_phase", "cleanup"])
async def test_failure_in_mutation_or_cleanup_rolls_back_every_table(postgresql, mode):
    """Failures after earlier phases never leave partial canonical state."""
    async with _hardened_environment(postgresql) as env:
        session_id = await _prepare_failure_case(env, mode)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            with pytest.raises(asyncpg.PostgresError):
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.commit_session(session_id, schema=env.schema)

            assert (
                env.setup.execute(
                    f'SELECT * FROM "{env.schema}".a_good_base'
                ).fetchall()
                == []
            )
            assert env.setup.execute(
                f'SELECT * FROM "{env.schema}".z_bad_base ORDER BY id'
            ).fetchall() == [(1, 1, "existing")]
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".a_good_changes '
                f"WHERE session_id = '{session_id}'::uuid"
            ).fetchone() == (1,)
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".z_bad_changes '
                f"WHERE session_id = '{session_id}'::uuid"
            ).fetchone() == (1,)
        finally:
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_fk_failure_rolls_back_prior_table_and_preserves_pending(postgresql):
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".a_good '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".y_parents ' "(id integer PRIMARY KEY)",
                f'CREATE TABLE "{env.schema}".z_children '
                "(id integer PRIMARY KEY, parent_id integer NOT NULL "
                f'REFERENCES "{env.schema}".y_parents(id))',
            ],
            ["a_good", "y_parents", "z_children"],
        )
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            async with asyncpg_cow_session(runtime_pool, session_id=session_id) as cow:
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".a_good VALUES (1, 'good')"
                )
                await cow.set_operation()
                await cow.execute(
                    f'INSERT INTO "{env.schema}".z_children VALUES (1, 999)'
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.commit_session(session_id, schema=env.schema)
            assert (
                env.setup.execute(
                    f'SELECT * FROM "{env.schema}".a_good_base'
                ).fetchall()
                == []
            )
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".z_children_changes '
                f"WHERE session_id = '{session_id}'::uuid"
            ).fetchone() == (1,)
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_conflict_maps_to_structured_error_and_rolls_back_all_tables(postgresql):
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".a_good '
                "(id integer PRIMARY KEY, value text NOT NULL)",
            ],
            ["a_good"],
        )
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            async with asyncpg_cow_session(runtime_pool, session_id=session_id) as cow:
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".a_good VALUES (1, 'pending')"
                )
                await cow.set_operation()
                await cow.execute(
                    f'UPDATE "{env.schema}".items ' "SET value = 'pending' WHERE id = 1"
                )
            env.setup.execute(
                f'UPDATE "{env.schema}".items_base '
                "SET value = 'canonical' WHERE id = 1"
            )
            env.setup.commit()

            with pytest.raises(CowConflictError) as caught:
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.commit_session(session_id, schema=env.schema)
            assert caught.value.sqlstate == "40001"
            assert len(caught.value.conflicts) == 1
            assert caught.value.conflicts[0]["table_name"] == "items"
            assert (
                env.setup.execute(
                    f'SELECT * FROM "{env.schema}".a_good_base'
                ).fetchall()
                == []
            )
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("canonical",)
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".a_good_changes '
                f"WHERE session_id = '{session_id}'::uuid"
            ).fetchone() == (1,)
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_selective_multitable_commit_rebases_survivor(postgresql):
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".alpha '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".beta '
                "(id integer PRIMARY KEY, value text NOT NULL)",
            ],
            ["alpha", "beta"],
        )
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            operation_a, operation_b, operation_c = (
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            async with asyncpg_cow_session(
                runtime_pool,
                session_id=session_id,
                operation_id=operation_a,
            ) as cow:
                await cow.execute(f"INSERT INTO \"{env.schema}\".alpha VALUES (1, 'A')")
                await cow.set_operation(operation_b)
                await cow.execute(f"INSERT INTO \"{env.schema}\".beta VALUES (1, 'B')")
                await cow.set_operation(operation_c)
                await cow.execute(
                    f'UPDATE "{env.schema}".alpha ' "SET value = 'C' WHERE id = 1"
                )

            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                result = await reviewer.commit_operations(
                    session_id,
                    [operation_a, operation_b],
                    schema=env.schema,
                )
            assert result.committed_tables == ("alpha", "beta")
            assert result.committed_operations == (operation_a, operation_b)
            assert result.has_pending_operations is True
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".alpha_base WHERE id = 1'
            ).fetchone() == ("A",)

            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                final = await reviewer.commit_operations(
                    session_id, [operation_c], schema=env.schema
                )
            assert final.has_pending_operations is False
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".alpha_base WHERE id = 1'
            ).fetchone() == ("C",)
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_selective_discard_is_atomic_and_rejects_invalid_dependency(postgresql):
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".alpha '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".beta '
                "(id integer PRIMARY KEY, value text NOT NULL)",
            ],
            ["alpha", "beta"],
        )
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            operation_a, operation_b, operation_c = (
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            async with asyncpg_cow_session(
                runtime_pool,
                session_id=session_id,
                operation_id=operation_a,
            ) as cow:
                await cow.execute(f"INSERT INTO \"{env.schema}\".alpha VALUES (1, 'A')")
                await cow.set_operation(operation_b)
                await cow.execute(f"INSERT INTO \"{env.schema}\".beta VALUES (1, 'B')")
                await cow.set_operation(operation_c)
                await cow.execute(
                    f'UPDATE "{env.schema}".alpha ' "SET value = 'C' WHERE id = 1"
                )

            with pytest.raises(CowPromotionRequestError):
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.commit_operations(
                        session_id, [operation_c], schema=env.schema
                    )

            with pytest.raises(CowPromotionRequestError):
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.discard_operations(
                        session_id, [operation_a], schema=env.schema
                    )

            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                result = await reviewer.discard_operations(
                    session_id, [operation_b], schema=env.schema
                )
            assert result.discarded_tables == ("beta",)
            assert result.discarded_operations == (operation_b,)
            assert result.has_pending_operations is True
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                await reviewer.commit_session(session_id, schema=env.schema)
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".alpha_base WHERE id = 1'
            ).fetchone() == ("C",)
            assert (
                env.setup.execute(f'SELECT * FROM "{env.schema}".beta_base').fetchall()
                == []
            )
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_selective_conflict_preserves_every_selected_table(postgresql):
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".alpha '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".beta '
                "(id integer PRIMARY KEY, value text NOT NULL)",
            ],
            ["alpha", "beta"],
        )
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            operation_a, operation_b = uuid.uuid4(), uuid.uuid4()
            async with asyncpg_cow_session(
                runtime_pool,
                session_id=session_id,
                operation_id=operation_a,
            ) as cow:
                await cow.execute(f"INSERT INTO \"{env.schema}\".alpha VALUES (1, 'A')")
                await cow.set_operation(operation_b)
                await cow.execute(f"INSERT INTO \"{env.schema}\".beta VALUES (1, 'B')")
            env.setup.execute(
                f"INSERT INTO \"{env.schema}\".beta_base VALUES (1, 'external')"
            )
            env.setup.commit()

            with pytest.raises(CowConflictError):
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.commit_operations(
                        session_id,
                        [operation_a, operation_b],
                        schema=env.schema,
                    )
            assert (
                env.setup.execute(f'SELECT * FROM "{env.schema}".alpha_base').fetchall()
                == []
            )
            assert env.setup.execute(
                f'SELECT * FROM "{env.schema}".beta_base'
            ).fetchall() == [(1, "external")]
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_cancellation_rolls_back_and_returns_clean_reusable_connection(
    postgresql,
):
    async with _hardened_environment(postgresql) as env:
        session_id = uuid.uuid4()
        await _record_item_update(env, session_id, "cancelled")
        env.setup.execute(
            f'CREATE FUNCTION "{env.schema}".slow_promotion() RETURNS trigger '
            "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(30); RETURN NEW; END $$"
        )
        env.setup.execute(
            f"CREATE TRIGGER slow_promotion BEFORE UPDATE ON "
            f'"{env.schema}".items_base FOR EACH ROW EXECUTE FUNCTION '
            f'"{env.schema}".slow_promotion()'
        )
        env.setup.commit()
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                task = asyncio.create_task(
                    reviewer.commit_session(session_id, schema=env.schema)
                )
                await asyncio.sleep(0.2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("one",)
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".items_changes '
                f"WHERE session_id = '{session_id}'::uuid"
            ).fetchone() == (1,)
            async with reviewer_pool.acquire() as connection:
                assert await connection.fetchval("SELECT 1") == 1
                assert connection.is_in_transaction() is False
                assert await _context(connection) == (None, None, None)
        finally:
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_overlapping_and_unrelated_concurrent_promotions_are_serializable(
    postgresql,
):
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".alpha '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".beta '
                "(id integer PRIMARY KEY, value text NOT NULL)",
            ],
            ["alpha", "beta"],
        )
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=2)
        runtime_pool = await _pool(env, env.runtime_role, max_size=2)
        try:
            first, second = uuid.uuid4(), uuid.uuid4()
            async with asyncpg_cow_session(runtime_pool, session_id=first) as cow:
                await cow.execute(
                    f"UPDATE \"{env.schema}\".items SET value = 'first' WHERE id = 1"
                )
            async with asyncpg_cow_session(runtime_pool, session_id=second) as cow:
                await cow.execute(
                    f"UPDATE \"{env.schema}\".items SET value = 'second' WHERE id = 1"
                )

            async def promote(session_id):
                try:
                    async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                        await reviewer.commit_session(session_id, schema=env.schema)
                    return "committed"
                except CowConflictError:
                    return "conflict"

            outcomes = await asyncio.wait_for(
                asyncio.gather(promote(first), promote(second)), timeout=10
            )
            assert sorted(outcomes) == ["committed", "conflict"]
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone()[0] in {"first", "second"}

            third, fourth = uuid.uuid4(), uuid.uuid4()
            async with asyncpg_cow_session(runtime_pool, session_id=third) as cow:
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".alpha VALUES (1, 'three')"
                )
            async with asyncpg_cow_session(runtime_pool, session_id=fourth) as cow:
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".beta VALUES (1, 'four')"
                )
            unrelated = await asyncio.wait_for(
                asyncio.gather(promote(third), promote(fourth)), timeout=10
            )
            assert unrelated == ["committed", "committed"]
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".alpha_base'
            ).fetchall() == [("three",)]
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".beta_base'
            ).fetchall() == [("four",)]
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_same_session_runtime_write_waits_until_promotion_finishes(postgresql):
    """The locked session set cannot grow underneath whole-session review."""
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".later '
                "(id integer PRIMARY KEY, value text NOT NULL)",
            ],
            ["later"],
        )
        session_id = uuid.uuid4()
        await _record_item_update(env, session_id, "promoted")
        env.setup.execute(
            f'CREATE FUNCTION "{env.schema}".pause_review() RETURNS trigger '
            "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1); RETURN NEW; END $$"
        )
        env.setup.execute(
            f"CREATE TRIGGER pause_review BEFORE UPDATE ON "
            f'"{env.schema}".items_base FOR EACH ROW EXECUTE FUNCTION '
            f'"{env.schema}".pause_review()'
        )
        env.setup.commit()
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        try:
            reviewer_pid: asyncio.Future[int] = (
                asyncio.get_running_loop().create_future()
            )

            async def promote():
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    reviewer_pid.set_result(
                        await reviewer.native.fetchval("SELECT pg_backend_pid()")
                    )
                    return await reviewer.commit_session(session_id, schema=env.schema)

            async def write_later():
                async with asyncpg_cow_session(
                    runtime_pool, session_id=session_id
                ) as cow:
                    await cow.execute(
                        f'INSERT INTO "{env.schema}".later '
                        "VALUES (1, 'after review')"
                    )

            promotion = asyncio.create_task(promote())
            pid = await asyncio.wait_for(reviewer_pid, timeout=5)
            for _ in range(100):
                locked = env.setup.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE pid = $1 AND locktype = 'advisory' "
                    "AND mode = 'ExclusiveLock' AND granted)",
                    pid,
                ).fetchone()
                if locked == (True,):
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("reviewer did not acquire the session advisory lock")
            writer = asyncio.create_task(write_later())
            await asyncio.sleep(0.2)
            assert writer.done() is False
            result, _ = await asyncio.wait_for(
                asyncio.gather(promotion, writer), timeout=10
            )
            assert result.committed_tables == ("items",)
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("promoted",)
            assert (
                env.setup.execute(f'SELECT * FROM "{env.schema}".later_base').fetchall()
                == []
            )
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".later_changes '
                f"WHERE session_id = '{session_id}'::uuid"
            ).fetchone() == ("after review",)
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_stale_context_and_existing_transactions_are_rejected_and_cleaned(
    postgresql,
):
    async with _hardened_environment(postgresql) as env:
        connection = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=env.reviewer_role,
            password=PG_PASSWORD,
            database=env.setup.info.dbname,
        )
        try:
            stale = uuid.uuid4()
            await connection.execute(f"SET app.session_id = '{stale}'")
            with pytest.raises(CowPromotionStateError, match="clean PostgreSQL"):
                async with asyncpg_cow_reviewer(connection):
                    pass
            assert await _context(connection) == (None, None, None)

            transaction = connection.transaction()
            await transaction.start()
            with pytest.raises(CowPromotionStateError, match="no active transaction"):
                async with asyncpg_cow_reviewer(connection):
                    pass
            await transaction.rollback()
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_reviewer_needs_no_internal_acl_and_runtime_cannot_promote(postgresql):
    async with _hardened_environment(postgresql) as env:
        session_id = uuid.uuid4()
        await _record_item_update(env, session_id, "reviewed")
        assert env.reviewer.execute(
            "SELECT has_table_privilege(current_user, "
            f"'{env.schema}.items_base', 'SELECT,INSERT,UPDATE,DELETE'), "
            "has_table_privilege(current_user, "
            f"'{env.schema}.items_changes', 'SELECT,INSERT,UPDATE,DELETE'), "
            "has_table_privilege(current_user, "
            f"'{env.schema}.cow_dirty_tables', 'SELECT,INSERT,UPDATE,DELETE')"
        ).fetchone() == (False, False, False)
        env.reviewer.rollback()

        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with asyncpg_cow_reviewer(runtime_pool) as reviewer:
                    await reviewer.commit_session(session_id, schema=env.schema)
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("one",)
        finally:
            await runtime_pool.close()


@pytest.mark.asyncio
async def test_sqlalchemy_reviewer_adapter_owns_atomic_transaction(postgresql):
    async with _hardened_environment(postgresql) as env:
        session_id = uuid.uuid4()
        await _record_item_update(env, session_id, "sqlalchemy")
        url = URL.create(
            "postgresql+asyncpg",
            username=env.reviewer_role,
            password=PG_PASSWORD,
            host=PG_HOST,
            port=PG_PORT,
            database=env.setup.info.dbname,
        )
        engine = create_async_engine(url)
        try:
            async with sqlalchemy_cow_reviewer(engine) as reviewer:
                result = await reviewer.commit_session(session_id, schema=env.schema)
            assert result.committed_tables == ("items",)
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("sqlalchemy",)
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_overwrite_policy_remains_available(postgresql):
    async with _hardened_environment(postgresql) as env:
        session_id = uuid.uuid4()
        await _record_item_update(env, session_id, "session")
        env.setup.execute(
            f'UPDATE "{env.schema}".items_base ' "SET value = 'canonical' WHERE id = 1"
        )
        env.setup.commit()
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                result = await reviewer.commit_session(
                    session_id,
                    schema=env.schema,
                    conflict_policy="overwrite",
                )
            assert result.conflict_policy == "overwrite"
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("session",)
        finally:
            await reviewer_pool.close()
