"""Regression coverage for first-touch optimistic conflict detection."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from agentcow.postgres import (
    apply_cow_variables,
    asyncpg_cow_session,
    commit_cow_operations,
    commit_cow_session,
    commit_cow_session_schema,
    deploy_cow_functions,
    discard_cow_operations,
    enable_cow,
    enable_cow_schema,
    get_cow_conflicts,
)
from agentcow.postgres.examples.asyncpg_safe_session_example import AsyncpgExecutor
from conftest import (
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    AsyncpgExecutor as _AsyncpgTestExecutor,
    connect_test_database,
)
from test_role_hardening import _assert_insufficient, _hardened_environment


async def _prepare(executor, *, schema_wide: bool = False) -> None:
    await deploy_cow_functions(executor)
    if schema_wide:
        await enable_cow_schema(executor)
    else:
        await enable_cow(executor, "users")
    executor.commit()


async def _record(executor, session_id: uuid.UUID, sql: str) -> uuid.UUID:
    operation_id = uuid.uuid4()
    await apply_cow_variables(executor, session_id, operation_id)
    await executor.execute(sql)
    return operation_id


async def _assert_conflicting_commit(
    executor,
    session_id: uuid.UUID,
    *,
    table_name: str = "users",
    schema: str = "public",
) -> None:
    with pytest.raises(asyncpg.SerializationError):
        await commit_cow_session(
            executor,
            table_name,
            session_id,
            schema=schema,
        )
    executor._conn.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_sql", "canonical_sql", "kind", "row_id", "canonical_rows"),
    (
        (
            "UPDATE users SET name = 'Session' WHERE id = 1",
            "UPDATE users_base SET name = 'Canonical' WHERE id = 1",
            "BASE_ROW_CHANGED",
            1,
            [("Canonical",)],
        ),
        (
            "UPDATE users SET name = 'Session' WHERE id = 50",
            "DELETE FROM users_base WHERE id = 50",
            "BASE_ROW_DELETED",
            50,
            [],
        ),
        (
            "DELETE FROM users WHERE id = 1",
            "UPDATE users_base SET name = 'Canonical' WHERE id = 1",
            "BASE_ROW_CHANGED",
            1,
            [("Canonical",)],
        ),
        (
            "INSERT INTO users (id, name, email) "
            "VALUES (100, 'Session', 'session@example.test')",
            "INSERT INTO users_base (id, name, email) "
            "VALUES (100, 'Canonical', 'canonical@example.test')",
            "BASE_ROW_CREATED",
            100,
            [("Canonical",)],
        ),
    ),
)
async def test_update_delete_and_insert_conflicts_preserve_both_states(
    seeded_executor,
    session_sql,
    canonical_sql,
    kind,
    row_id,
    canonical_rows,
):
    """Every existing/absent-row conflict preserves canonical and pending state."""
    await _prepare(seeded_executor)
    if row_id == 50:
        await seeded_executor.execute(
            "INSERT INTO users_base (id, name, email) "
            "VALUES (50, 'Unreferenced', 'unreferenced@example.test')"
        )
        seeded_executor.commit()
    session_id = uuid.uuid4()
    await _record(seeded_executor, session_id, session_sql)
    seeded_executor.commit()

    await seeded_executor.execute(canonical_sql)
    seeded_executor.commit()

    conflicts = await get_cow_conflicts(seeded_executor, session_id)
    assert [conflict["conflict_kind"] for conflict in conflicts] == [kind]
    assert conflicts[0]["primary_key"] == {"id": row_id}
    seeded_executor._conn.rollback()

    await _assert_conflicting_commit(seeded_executor, session_id)
    assert (
        await seeded_executor.execute(
            f"SELECT name FROM users_base WHERE id = {row_id}"
        )
        == canonical_rows
    )
    assert await seeded_executor.execute(
        f"SELECT count(*) FROM users_changes WHERE session_id = '{session_id}'::uuid"
    ) == [(1,)]


@pytest.mark.asyncio
async def test_two_sessions_same_row_conflict_but_unrelated_rows_do_not(
    seeded_executor,
):
    """One accepted session invalidates only competing first-touch baselines."""
    await _prepare(seeded_executor)
    first = uuid.uuid4()
    second = uuid.uuid4()
    unrelated = uuid.uuid4()

    await _record(
        seeded_executor, first, "UPDATE users SET name = 'First' WHERE id = 1"
    )
    await _record(
        seeded_executor, second, "UPDATE users SET name = 'Second' WHERE id = 1"
    )
    await _record(
        seeded_executor,
        unrelated,
        "UPDATE users SET name = 'Unrelated' WHERE id = 2",
    )
    seeded_executor.commit()

    await commit_cow_session(seeded_executor, "users", first)
    seeded_executor.commit()
    await _assert_conflicting_commit(seeded_executor, second)

    await commit_cow_session(seeded_executor, "users", unrelated)
    seeded_executor.commit()
    assert await seeded_executor.execute(
        "SELECT id, name FROM users_base ORDER BY id"
    ) == [(1, "First"), (2, "Unrelated")]
    assert await seeded_executor.execute(
        f"SELECT count(*) FROM users_changes WHERE session_id = '{second}'::uuid"
    ) == [(1,)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_sql", "second_sql", "row_id", "expected_exists"),
    (
        (
            "UPDATE users SET name = 'First' WHERE id = 1",
            "UPDATE users SET name = 'Second' WHERE id = 1",
            1,
            True,
        ),
        (
            "UPDATE users SET name = 'First' WHERE id = 1",
            "DELETE FROM users WHERE id = 1",
            1,
            True,
        ),
        (
            "DELETE FROM users WHERE id = 1",
            "INSERT INTO users (id, name, email) "
            "VALUES (1, 'Reinserted', 'bessie@sunnymeadow.farm')",
            1,
            True,
        ),
        (
            "INSERT INTO users (id, name, email) "
            "VALUES (100, 'First', 'first@example.test')",
            "UPDATE users SET name = 'Second' WHERE id = 100",
            100,
            False,
        ),
        (
            "INSERT INTO users (id, name, email) "
            "VALUES (100, 'First', 'first@example.test')",
            "DELETE FROM users WHERE id = 100",
            100,
            False,
        ),
    ),
)
async def test_operation_chains_retain_the_first_touch_baseline(
    seeded_executor,
    first_sql,
    second_sql,
    row_id,
    expected_exists,
):
    """Later operation IDs never refresh a same-row session baseline."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    await seeded_executor.execute("BEGIN")
    await _record(seeded_executor, session_id, first_sql)
    await _record(seeded_executor, session_id, second_sql)

    rows = await seeded_executor.execute(
        "SELECT _cow_base_exists, _cow_base_row, _cow_base_schema "
        "FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid AND id = {row_id} "
        "ORDER BY _cow_order"
    )
    assert len(rows) == 2
    assert all(row[0] is expected_exists for row in rows)
    assert rows[0][1:] == rows[1][1:]
    if expected_exists:
        assert rows[0][1]["name"] == "Bessie"
    else:
        assert rows[0][1] is None


@pytest.mark.asyncio
async def test_same_operation_rewrite_preserves_original_baseline(seeded_executor):
    """The change-row ON CONFLICT path updates state, not its baseline."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    await apply_cow_variables(seeded_executor, session_id, operation_id)
    await seeded_executor.execute("UPDATE users SET name = 'First' WHERE id = 1")
    await seeded_executor.execute("UPDATE users SET name = 'Second' WHERE id = 1")
    assert await seeded_executor.execute(
        "SELECT name, _cow_base_row->>'name' FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid AND id = 1"
    ) == [("Second", "Bessie")]


@pytest.mark.asyncio
async def test_current_state_returning_to_baseline_is_not_a_conflict(seeded_executor):
    """H06 compares current state, not an unknowable history of mutations."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'Session' WHERE id = 1"
    )
    seeded_executor.commit()

    await seeded_executor.execute(
        "UPDATE users_base SET name = 'Temporary' WHERE id = 1"
    )
    await seeded_executor.execute("UPDATE users_base SET name = 'Bessie' WHERE id = 1")
    seeded_executor.commit()

    assert await get_cow_conflicts(seeded_executor, session_id) == []
    seeded_executor._conn.rollback()
    await commit_cow_session(seeded_executor, "users", session_id)
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 1"
    ) == [("Session",)]


@pytest.mark.asyncio
async def test_explicit_overwrite_policy_restores_legacy_last_writer_wins(
    seeded_executor,
):
    """Overwrite behavior requires an unmistakable caller choice."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'Session' WHERE id = 1"
    )
    seeded_executor.commit()
    await seeded_executor.execute(
        "UPDATE users_base SET name = 'Canonical' WHERE id = 1"
    )
    seeded_executor.commit()

    await commit_cow_session(
        seeded_executor,
        "users",
        session_id,
        conflict_policy="overwrite",
    )
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 1"
    ) == [("Session",)]
    with pytest.raises(ValueError, match="conflict_policy"):
        await commit_cow_session(
            seeded_executor,
            "users",
            uuid.uuid4(),
            conflict_policy="silent",
        )


@pytest.mark.asyncio
async def test_selective_commit_rebases_surviving_later_operations(seeded_executor):
    """Accepting a causal prefix rebases later work onto that accepted state."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    first = await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'First' WHERE id = 1"
    )
    await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'Second' WHERE id = 1"
    )
    seeded_executor.commit()

    await commit_cow_operations(seeded_executor, "users", session_id, [first])
    seeded_executor.commit()
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 1"
    ) == [("First",)]
    assert await seeded_executor.execute(
        "SELECT name, _cow_base_row->>'name' FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid"
    ) == [("Second", "First")]
    assert await get_cow_conflicts(seeded_executor, session_id) == []
    seeded_executor._conn.rollback()

    await commit_cow_session(seeded_executor, "users", session_id)
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 1"
    ) == [("Second",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", ("delete-reinsert", "insert-delete"))
async def test_selective_commit_rebases_across_delete_and_insert_phases(
    seeded_executor,
    chain,
):
    """Rebasing follows canonical existence through either commit phase."""
    if chain == "delete-reinsert":
        await seeded_executor.execute(
            "INSERT INTO users (id, name, email) "
            "VALUES (50, 'Original', 'original@example.test')"
        )
        seeded_executor.commit()
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()

    if chain == "delete-reinsert":
        first = await _record(
            seeded_executor, session_id, "DELETE FROM users WHERE id = 50"
        )
        await _record(
            seeded_executor,
            session_id,
            "INSERT INTO users (id, name, email) "
            "VALUES (50, 'Reinserted', 'reinserted@example.test')",
        )
        row_id = 50
        expected_baseline = (False, None)
        expected_final = [("Reinserted",)]
    else:
        first = await _record(
            seeded_executor,
            session_id,
            "INSERT INTO users (id, name, email) "
            "VALUES (100, 'Inserted', 'inserted@example.test')",
        )
        await _record(seeded_executor, session_id, "DELETE FROM users WHERE id = 100")
        row_id = 100
        expected_baseline = (True, "Inserted")
        expected_final = []
    seeded_executor.commit()

    await commit_cow_operations(seeded_executor, "users", session_id, [first])
    seeded_executor.commit()
    assert await seeded_executor.execute(
        "SELECT _cow_base_exists, _cow_base_row->>'name' FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid AND id = {row_id}"
    ) == [expected_baseline]
    assert await get_cow_conflicts(seeded_executor, session_id) == []
    seeded_executor._conn.rollback()
    await commit_cow_session(seeded_executor, "users", session_id)
    assert (
        await seeded_executor.execute(
            f"SELECT name FROM users_base WHERE id = {row_id}"
        )
        == expected_final
    )


@pytest.mark.asyncio
async def test_selective_discard_preserves_original_baseline(seeded_executor):
    """Discarding a predecessor does not manufacture a newer baseline."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    first = await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'First' WHERE id = 1"
    )
    await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'Second' WHERE id = 1"
    )
    seeded_executor.commit()

    await discard_cow_operations(seeded_executor, "users", session_id, [first])
    assert await seeded_executor.execute(
        "SELECT _cow_base_row->>'name' FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid"
    ) == [("Bessie",)]
    await commit_cow_session(seeded_executor, "users", session_id)
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 1"
    ) == [("Second",)]


@pytest.mark.asyncio
async def test_selective_commit_rejects_external_conflict_and_non_prefix(
    seeded_executor,
):
    """Selective promotion remains conflict-safe and causally ordered."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    first = await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'First' WHERE id = 1"
    )
    second = await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'Second' WHERE id = 1"
    )
    seeded_executor.commit()

    with pytest.raises(asyncpg.InvalidParameterValueError, match="causal prefix"):
        await commit_cow_operations(seeded_executor, "users", session_id, [second])
    seeded_executor._conn.rollback()

    await seeded_executor.execute(
        "UPDATE users_base SET name = 'External' WHERE id = 1"
    )
    seeded_executor.commit()
    with pytest.raises(asyncpg.SerializationError):
        await commit_cow_operations(seeded_executor, "users", session_id, [first])
    seeded_executor._conn.rollback()
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 1"
    ) == [("External",)]
    assert await seeded_executor.execute(
        f"SELECT count(*) FROM users_changes WHERE session_id = '{session_id}'::uuid"
    ) == [(2,)]


@pytest.mark.asyncio
async def test_cross_table_conflict_is_reported_and_transaction_can_roll_back(
    seeded_executor,
):
    """One conflicting table aborts an explicitly transacted schema promotion."""
    await _prepare(seeded_executor, schema_wide=True)
    session_id = uuid.uuid4()
    await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'Session' WHERE id = 1"
    )
    await _record(
        seeded_executor,
        session_id,
        "UPDATE projects SET title = 'Session project' WHERE id = 1",
    )
    seeded_executor.commit()
    await seeded_executor.execute(
        "UPDATE projects_base SET title = 'Canonical project' WHERE id = 1"
    )
    seeded_executor.commit()

    conflicts = await get_cow_conflicts(seeded_executor, session_id)
    assert [(item["table_name"], item["conflict_kind"]) for item in conflicts] == [
        ("projects", "BASE_ROW_CHANGED")
    ]
    seeded_executor._conn.rollback()

    with pytest.raises(asyncpg.SerializationError):
        await commit_cow_session_schema(seeded_executor, session_id)
    seeded_executor._conn.rollback()
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 1"
    ) == [("Bessie",)]
    assert await seeded_executor.execute(
        "SELECT title FROM projects_base WHERE id = 1"
    ) == [("Canonical project",)]


@pytest.mark.asyncio
async def test_pending_schema_change_fails_conservatively(seeded_executor):
    """A changed base schema cannot be silently compared as if unchanged."""
    await _prepare(seeded_executor)
    session_id = uuid.uuid4()
    await _record(
        seeded_executor, session_id, "UPDATE users SET name = 'Session' WHERE id = 1"
    )
    seeded_executor.commit()
    await seeded_executor.execute("ALTER TABLE users_base ADD COLUMN note text")
    seeded_executor.commit()

    conflicts = await get_cow_conflicts(seeded_executor, session_id)
    assert [item["conflict_kind"] for item in conflicts] == ["BASE_SCHEMA_CHANGED"]
    seeded_executor._conn.rollback()
    await _assert_conflicting_commit(seeded_executor, session_id)


@pytest.mark.asyncio
async def test_composite_primary_key_conflict_is_structured(executor):
    """Conflict joins and reviewer details cover every primary-key column."""
    await executor.execute(
        "CREATE TABLE inventory ("
        "site text NOT NULL, sku text NOT NULL, quantity integer NOT NULL, "
        "PRIMARY KEY (site, sku))"
    )
    await executor.execute("INSERT INTO inventory VALUES ('north', 'hay', 10)")
    await deploy_cow_functions(executor)
    await enable_cow(executor, "inventory")
    executor.commit()

    session_id = uuid.uuid4()
    await _record(
        executor,
        session_id,
        "UPDATE inventory SET quantity = 9 WHERE site = 'north' AND sku = 'hay'",
    )
    executor.commit()
    await executor.execute(
        "UPDATE inventory_base SET quantity = 8 WHERE site = 'north' AND sku = 'hay'"
    )
    executor.commit()

    conflicts = await get_cow_conflicts(executor, session_id)
    assert conflicts[0]["primary_key"] == {"site": "north", "sku": "hay"}
    assert conflicts[0]["conflict_kind"] == "BASE_ROW_CHANGED"
    executor._conn.rollback()
    await _assert_conflicting_commit(
        executor,
        session_id,
        table_name="inventory",
    )


H05_SCHEMA_SQL = """
CREATE TABLE legacy_h05_base (
    id integer PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE legacy_h05_changes (
    session_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    id integer NOT NULL,
    value text NOT NULL,
    _cow_deleted boolean NOT NULL DEFAULT false,
    _cow_updated_at timestamptz NOT NULL DEFAULT now(),
    _cow_order bigint NOT NULL,
    PRIMARY KEY (session_id, operation_id, id)
);
CREATE VIEW legacy_h05 AS SELECT id, value FROM legacy_h05_base;
"""


@pytest.mark.asyncio
async def test_empty_h05_changes_table_migrates_conflict_metadata(executor):
    """An empty H05 enabled table receives baseline columns automatically."""
    await executor.execute(H05_SCHEMA_SQL)
    executor.commit()
    await deploy_cow_functions(executor)
    assert await executor.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'legacy_h05_changes' "
        "AND column_name LIKE '_cow_base_%' ORDER BY column_name"
    ) == [
        ("_cow_base_exists", "NO"),
        ("_cow_base_row", "YES"),
        ("_cow_base_schema", "NO"),
    ]

    executor.commit()
    await _record(executor, uuid.uuid4(), "INSERT INTO legacy_h05 VALUES (1, 'new')")
    assert await executor.execute(
        "SELECT _cow_base_exists, _cow_base_row FROM legacy_h05_changes"
    ) == [(False, None)]


@pytest.mark.asyncio
async def test_pending_h05_changes_refuse_ambiguous_migration(executor):
    """Deployment never labels current canonical state as a historical baseline."""
    await executor.execute(H05_SCHEMA_SQL)
    await executor.execute(
        "INSERT INTO legacy_h05_changes "
        "(session_id, operation_id, id, value, _cow_order) VALUES "
        f"('{uuid.uuid4()}'::uuid, '{uuid.uuid4()}'::uuid, 1, 'pending', 1)"
    )
    executor.commit()

    with pytest.raises(RuntimeError, match="pending legacy changes from before H06"):
        await deploy_cow_functions(executor)
    assert await executor.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'legacy_h05_changes' "
        "AND column_name LIKE '_cow_base_%'"
    ) == [(0,)]
    assert await executor.execute("SELECT count(*) FROM legacy_h05_changes") == [(1,)]


@pytest.mark.asyncio
async def test_hardened_reviewer_can_inspect_but_runtime_cannot(postgresql):
    """H03 controls expose conflicts without exposing their backing columns."""
    async with _hardened_environment(postgresql) as env:
        session_id = uuid.uuid4()
        runtime_executor = _AsyncpgTestExecutor(env.runtime)
        with env.runtime.transaction():
            await _record(
                runtime_executor,
                session_id,
                f"UPDATE \"{env.schema}\".items SET value = 'pending' WHERE id = 1",
            )
        env.setup.execute(
            f"UPDATE \"{env.schema}\".items_base SET value = 'canonical' WHERE id = 1"
        )
        env.setup.commit()

        conflicts = await get_cow_conflicts(
            env.reviewer_executor,
            session_id,
            schema=env.schema,
        )
        assert [(item["primary_key"], item["conflict_kind"]) for item in conflicts] == [
            ({"id": 1}, "BASE_ROW_CHANGED")
        ]
        env.reviewer.rollback()

        _assert_insufficient(
            env.runtime,
            f'SELECT _cow_base_row FROM "{env.schema}".items_changes',
        )
        _assert_insufficient(
            env.runtime,
            "SELECT * FROM agentcow.get_cow_conflicts("
            f"'{env.schema}', 'items_base', ARRAY['id'], "
            f"'{session_id}'::uuid, NULL::uuid[], NULL::boolean)",
        )
        with pytest.raises(asyncpg.SerializationError):
            await commit_cow_session(
                env.reviewer_executor,
                "items",
                session_id,
                schema=env.schema,
            )
        env.reviewer.rollback()
        assert env.setup.execute(
            f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
        ).fetchone() == ("canonical",)
        assert env.setup.execute(
            f'SELECT count(*) FROM "{env.schema}".items_changes '
            f"WHERE session_id = '{session_id}'::uuid"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_h04_safe_session_captures_baseline_automatically(postgresql):
    """The recommended runtime API needs no explicit baseline call."""
    async with _hardened_environment(postgresql) as env:
        pool = await asyncpg.create_pool(
            host=PG_HOST,
            port=PG_PORT,
            user=env.runtime_role,
            password=PG_PASSWORD,
            database=env.setup.info.dbname,
            min_size=1,
            max_size=1,
        )
        session_id = uuid.uuid4()
        try:
            async with asyncpg_cow_session(pool, session_id=session_id) as cow:
                await cow.execute(
                    f"UPDATE \"{env.schema}\".items SET value = 'pending' WHERE id = 1"
                )
        finally:
            await pool.close()

        assert env.setup.execute(
            "SELECT _cow_base_exists, _cow_base_row->>'value' "
            f'FROM "{env.schema}".items_changes '
            f"WHERE session_id = '{session_id}'::uuid"
        ).fetchone() == (True, "one")


@pytest.mark.asyncio
async def test_asyncpg_executor_can_deploy_h06_functions(postgresql):
    """H06 deployment statements remain valid for asyncpg's one-command API."""
    connection = await asyncpg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=postgresql.info.dbname,
    )
    try:
        await deploy_cow_functions(AsyncpgExecutor(connection))
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_atomic_commit_waits_for_inflight_canonical_writer(postgresql):
    """Validation and mutation share a lock boundary with canonical DML."""
    executor = _AsyncpgTestExecutor(postgresql)
    await deploy_cow_functions(executor)
    await executor.execute(
        "CREATE TABLE race_items (id integer PRIMARY KEY, value text NOT NULL)"
    )
    await executor.execute("INSERT INTO race_items VALUES (1, 'baseline')")
    await enable_cow(executor, "race_items")
    executor.commit()
    session_id = uuid.uuid4()
    await _record(
        executor,
        session_id,
        "UPDATE race_items SET value = 'session' WHERE id = 1",
    )
    executor.commit()

    database = postgresql.info.dbname
    canonical = connect_test_database(
        database,
        application_name="h06-canonical-writer",
    )
    canonical.execute("UPDATE race_items_base SET value = 'canonical' WHERE id = 1")

    async def promote() -> str | None:
        reviewer = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=database,
            server_settings={"application_name": "h06-reviewer"},
        )
        try:
            try:
                await reviewer.execute(
                    "SELECT agentcow.commit_cow("
                    "'public', 'race_items_base', ARRAY['id'], "
                    f"'{session_id}'::uuid, NULL::uuid[], 'error')"
                )
            except asyncpg.PostgresError as exc:
                return exc.sqlstate
            return None
        finally:
            await reviewer.close()

    promotion = asyncio.create_task(promote())
    observer = await asyncpg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=database,
    )
    try:
        for _ in range(200):
            waiting = await observer.fetchrow(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity "
                "WHERE application_name = 'h06-reviewer'"
            )
            if waiting is not None and waiting[0] is True:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("reviewer did not block behind the canonical writer")

        canonical.commit()
        assert await promotion == "40001"
    finally:
        canonical.close()
        await observer.close()

    assert postgresql.execute(
        "SELECT value FROM race_items_base WHERE id = 1"
    ).fetchone() == ("canonical",)
    assert postgresql.execute(
        f"SELECT value FROM race_items_changes WHERE session_id = '{session_id}'::uuid"
    ).fetchone() == ("session",)
