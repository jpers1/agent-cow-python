"""Regression coverage for deterministic PostgreSQL COW operation ordering."""

from __future__ import annotations

import uuid

import pytest

from agentcow.postgres import (
    apply_cow_variables,
    commit_cow_operations,
    commit_cow_session,
    deploy_cow_functions,
    disable_cow,
    disable_cow_schema,
    discard_cow_operations,
    enable_cow,
    enable_cow_schema,
    get_operation_dependencies,
    get_session_operations,
    set_visible_operations,
)

CASES = ("insert-delete", "delete-reinsert", "update-delete", "update-update")


async def _begin_ordering_test(executor, *, schema_wide: bool = False) -> None:
    await deploy_cow_functions(executor)
    if schema_wide:
        await enable_cow_schema(executor)
    else:
        await enable_cow(executor, "users")
    await executor.execute(
        "INSERT INTO users_base (id, name, email) "
        "VALUES (50, 'Unreferenced', 'unreferenced@example.test')"
    )
    executor.commit()
    await executor.execute("BEGIN")


async def _set_operation(executor, session_id, operation_id) -> None:
    await apply_cow_variables(executor, session_id, operation_id)


async def _apply_chain(executor, case: str, session_id):
    first_operation = uuid.uuid4()
    second_operation = uuid.uuid4()
    row_id = {
        "insert-delete": 100,
        "update-delete": 50,
    }.get(case, 1)

    await _set_operation(executor, session_id, first_operation)
    if case == "insert-delete":
        await executor.execute(
            "INSERT INTO users (id, name, email) "
            "VALUES (100, 'Transient', 'transient@example.test')"
        )
    elif case == "delete-reinsert":
        await executor.execute("DELETE FROM users WHERE id = 1")
    else:
        await executor.execute(f"UPDATE users SET name = 'First' WHERE id = {row_id}")

    await _set_operation(executor, session_id, second_operation)
    if case in {"insert-delete", "update-delete"}:
        await executor.execute(f"DELETE FROM users WHERE id = {row_id}")
    elif case == "delete-reinsert":
        await executor.execute(
            "INSERT INTO users (id, name, email) "
            "VALUES (1, 'Reinserted', 'bessie@sunnymeadow.farm')"
        )
    else:
        await executor.execute("UPDATE users SET name = 'Second' WHERE id = 1")

    return first_operation, second_operation, row_id


async def _assert_chain_order(executor, session_id, row_id) -> None:
    rows = await executor.execute(
        "SELECT operation_id, _cow_updated_at, _cow_order "
        "FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid AND id = {row_id} "
        "ORDER BY _cow_order"
    )
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1], "now() should tie inside one transaction"
    assert rows[0][2] < rows[1][2]


def _expected_name(case: str) -> str | None:
    if case in {"insert-delete", "update-delete"}:
        return None
    if case == "delete-reinsert":
        return "Reinserted"
    return "Second"


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", range(3))
@pytest.mark.parametrize("case", CASES)
async def test_same_transaction_overlay_uses_latest_order(
    seeded_executor, case, repeat
):
    """Timestamp ties never make preview state depend on physical row order."""
    del repeat
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    _, _, row_id = await _apply_chain(seeded_executor, case, session_id)

    await _assert_chain_order(seeded_executor, session_id, row_id)
    rows = await seeded_executor.execute(f"SELECT name FROM users WHERE id = {row_id}")
    expected = _expected_name(case)
    assert rows == ([] if expected is None else [(expected,)])
    await seeded_executor.execute("ROLLBACK")


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", range(3))
@pytest.mark.parametrize("case", CASES)
async def test_same_transaction_full_commit_uses_latest_order(
    seeded_executor, case, repeat
):
    """Full commit has the same latest-state result as the session overlay."""
    del repeat
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    _, _, row_id = await _apply_chain(seeded_executor, case, session_id)
    await _assert_chain_order(seeded_executor, session_id, row_id)
    seeded_executor.commit()

    await commit_cow_session(seeded_executor, "users", session_id)
    rows = await seeded_executor.execute(
        f"SELECT name FROM users_base WHERE id = {row_id}"
    )
    expected = _expected_name(case)
    assert rows == ([] if expected is None else [(expected,)])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_commit_upsert_and_delete_phases_use_same_latest_row(
    seeded_executor, case
):
    """The upsert phase never applies a stale row hidden by a later delete."""
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    _, _, row_id = await _apply_chain(seeded_executor, case, session_id)
    seeded_executor.commit()

    await seeded_executor.execute(
        "SELECT agentcow.commit_cow_upsert("
        f"'public', 'users_base', ARRAY['id'], '{session_id}'::uuid)"
    )
    upsert_rows = await seeded_executor.execute(
        f"SELECT name FROM users_base WHERE id = {row_id}"
    )
    upsert_expected = {
        "insert-delete": None,
        "delete-reinsert": "Reinserted",
        "update-delete": "Unreferenced",
        "update-update": "Second",
    }[case]
    assert upsert_rows == ([] if upsert_expected is None else [(upsert_expected,)])

    await seeded_executor.execute(
        "SELECT agentcow.commit_cow_delete("
        f"'public', 'users_base', ARRAY['id'], '{session_id}'::uuid)"
    )
    final_rows = await seeded_executor.execute(
        f"SELECT name FROM users_base WHERE id = {row_id}"
    )
    expected = _expected_name(case)
    assert final_rows == ([] if expected is None else [(expected,)])


@pytest.mark.asyncio
async def test_on_conflict_rewrite_consumes_a_new_order(seeded_executor):
    """Repeated writes in one operation update the stored causal order."""
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    await _set_operation(seeded_executor, session_id, operation_id)

    await seeded_executor.execute("UPDATE users SET name = 'First' WHERE id = 1")
    first_order = (
        await seeded_executor.execute(
            "SELECT _cow_order FROM users_changes "
            f"WHERE session_id = '{session_id}'::uuid AND id = 1"
        )
    )[0][0]
    await seeded_executor.execute("UPDATE users SET name = 'Second' WHERE id = 1")
    rows = await seeded_executor.execute(
        "SELECT name, _cow_order FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid AND id = 1"
    )

    assert len(rows) == 1
    assert rows[0][0] == "Second"
    assert rows[0][1] > first_order


@pytest.mark.asyncio
async def test_selective_commit_preserves_operation_filter(seeded_executor):
    """Selecting the earlier insert commits it without selecting its delete."""
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    insert_operation, _, _ = await _apply_chain(
        seeded_executor, "insert-delete", session_id
    )
    seeded_executor.commit()

    await commit_cow_operations(
        seeded_executor, "users", session_id, [insert_operation]
    )
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 100"
    ) == [("Transient",)]
    assert await seeded_executor.execute(
        "SELECT _cow_deleted FROM users_changes "
        f"WHERE session_id = '{session_id}'::uuid AND id = 100"
    ) == [(True,)]


@pytest.mark.asyncio
async def test_selective_discard_preserves_remaining_order(seeded_executor):
    """Discarding a later delete reveals and preserves the earlier insert."""
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    _, delete_operation, _ = await _apply_chain(
        seeded_executor, "insert-delete", session_id
    )
    seeded_executor.commit()

    await discard_cow_operations(
        seeded_executor, "users", session_id, [delete_operation]
    )
    await _set_operation(seeded_executor, session_id, uuid.uuid4())
    assert await seeded_executor.execute("SELECT name FROM users WHERE id = 100") == [
        ("Transient",)
    ]
    seeded_executor.commit()
    await commit_cow_session(seeded_executor, "users", session_id)
    assert await seeded_executor.execute(
        "SELECT name FROM users_base WHERE id = 100"
    ) == [("Transient",)]


@pytest.mark.asyncio
async def test_visible_operations_use_order_with_timestamp_ties(seeded_executor):
    """Visibility filters select operations without reintroducing ambiguity."""
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    first_operation, second_operation, _ = await _apply_chain(
        seeded_executor, "update-update", session_id
    )

    await set_visible_operations(seeded_executor, [first_operation])
    assert await seeded_executor.execute("SELECT name FROM users WHERE id = 1") == [
        ("First",)
    ]
    await set_visible_operations(seeded_executor, [second_operation])
    assert await seeded_executor.execute("SELECT name FROM users WHERE id = 1") == [
        ("Second",)
    ]
    await set_visible_operations(seeded_executor, [first_operation, second_operation])
    assert await seeded_executor.execute("SELECT name FROM users WHERE id = 1") == [
        ("Second",)
    ]


@pytest.mark.asyncio
async def test_same_row_dependency_uses_order_not_timestamp(seeded_executor):
    """A later same-row operation depends on its predecessor despite a tie."""
    await _begin_ordering_test(seeded_executor)
    session_id = uuid.uuid4()
    first_operation, second_operation, _ = await _apply_chain(
        seeded_executor, "update-update", session_id
    )

    dependencies = await get_operation_dependencies(seeded_executor, session_id)
    assert (first_operation, second_operation) in dependencies
    assert (second_operation, first_operation) not in dependencies

    operations = await get_session_operations(seeded_executor, session_id)
    assert operations.index(first_operation) < operations.index(second_operation)


@pytest.mark.asyncio
async def test_fk_dependency_uses_schema_wide_order(seeded_executor):
    """Related tables share one order domain for FK dependency recovery."""
    await _begin_ordering_test(seeded_executor, schema_wide=True)
    session_id = uuid.uuid4()
    parent_operation = uuid.uuid4()
    child_operation = uuid.uuid4()

    await _set_operation(seeded_executor, session_id, parent_operation)
    await seeded_executor.execute(
        "INSERT INTO users (id, name, email) "
        "VALUES (100, 'Parent', 'parent@example.test')"
    )
    await _set_operation(seeded_executor, session_id, child_operation)
    await seeded_executor.execute(
        "INSERT INTO projects (id, owner_id, title) VALUES (100, 100, 'Child')"
    )

    parent_row = (
        await seeded_executor.execute(
            "SELECT _cow_updated_at, _cow_order FROM users_changes "
            f"WHERE session_id = '{session_id}'::uuid AND id = 100"
        )
    )[0]
    child_row = (
        await seeded_executor.execute(
            "SELECT _cow_updated_at, _cow_order FROM projects_changes "
            f"WHERE session_id = '{session_id}'::uuid AND id = 100"
        )
    )[0]
    assert parent_row[0] == child_row[0]
    assert parent_row[1] < child_row[1]

    dependencies = await get_operation_dependencies(seeded_executor, session_id)
    assert (parent_operation, child_operation) in dependencies


@pytest.mark.asyncio
async def test_rollback_gap_does_not_affect_later_writes(seeded_executor):
    """A rolled-back nextval remains a harmless gap in deterministic order."""
    await _begin_ordering_test(seeded_executor)
    rolled_back_session = uuid.uuid4()
    await _set_operation(seeded_executor, rolled_back_session, uuid.uuid4())
    await seeded_executor.execute(
        "INSERT INTO users (id, name, email) "
        "VALUES (100, 'Rolled back', 'rollback@example.test')"
    )
    rolled_back_order = (
        await seeded_executor.execute(
            "SELECT _cow_order FROM users_changes WHERE id = 100"
        )
    )[0][0]
    await seeded_executor.execute("ROLLBACK")

    await seeded_executor.execute("BEGIN")
    later_session = uuid.uuid4()
    await _set_operation(seeded_executor, later_session, uuid.uuid4())
    await seeded_executor.execute(
        "INSERT INTO users (id, name, email) "
        "VALUES (101, 'After rollback', 'after-rollback@example.test')"
    )
    later_order = (
        await seeded_executor.execute(
            "SELECT _cow_order FROM users_changes WHERE id = 101"
        )
    )[0][0]

    assert later_order > rolled_back_order
    assert await seeded_executor.execute("SELECT name FROM users WHERE id = 101") == [
        ("After rollback",)
    ]


LEGACY_SCHEMA_SQL = """
CREATE TABLE legacy_accounts_base (
    id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE legacy_accounts_changes (
    session_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    id integer NOT NULL,
    name text NOT NULL,
    _cow_deleted boolean NOT NULL DEFAULT false,
    _cow_updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, operation_id, id)
);
CREATE VIEW legacy_accounts AS SELECT id, name FROM legacy_accounts_base;
"""


@pytest.mark.asyncio
async def test_empty_upstream_changes_table_is_upgraded(executor):
    """Deployment automatically migrates an empty upstream-format table."""
    await executor.execute(LEGACY_SCHEMA_SQL)
    executor.commit()

    await deploy_cow_functions(executor)
    columns = await executor.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "AND table_name = 'legacy_accounts_changes' "
        "AND column_name = '_cow_order'"
    )
    assert columns == [("bigint", "NO")]

    executor.commit()
    await executor.execute("BEGIN")
    session_id = uuid.uuid4()
    await _set_operation(executor, session_id, uuid.uuid4())
    await executor.execute("INSERT INTO legacy_accounts VALUES (1, 'First')")
    await _set_operation(executor, session_id, uuid.uuid4())
    await executor.execute("UPDATE legacy_accounts SET name = 'Second' WHERE id = 1")
    assert await executor.execute("SELECT name FROM legacy_accounts WHERE id = 1") == [
        ("Second",)
    ]


@pytest.mark.asyncio
async def test_pending_upstream_changes_block_upgrade(executor):
    """Deployment refuses to invent order for pending upstream-format rows."""
    await executor.execute(LEGACY_SCHEMA_SQL)
    await executor.execute(
        "INSERT INTO legacy_accounts_changes "
        "(session_id, operation_id, id, name) VALUES "
        f"('{uuid.uuid4()}'::uuid, '{uuid.uuid4()}'::uuid, 1, 'First'), "
        f"('{uuid.uuid4()}'::uuid, '{uuid.uuid4()}'::uuid, 2, 'Second')"
    )
    executor.commit()

    assert await executor.execute(
        "SELECT count(DISTINCT _cow_updated_at) FROM legacy_accounts_changes"
    ) == [(1,)]

    with pytest.raises(RuntimeError, match="contains pending legacy changes"):
        await deploy_cow_functions(executor)

    assert await executor.execute("SELECT count(*) FROM legacy_accounts_changes") == [
        (2,)
    ]
    assert await executor.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "AND table_name = 'legacy_accounts_changes' "
        "AND column_name = '_cow_order'"
    ) == [(0,)]


@pytest.mark.asyncio
async def test_schema_sequence_owner_and_lifecycle(seeded_executor):
    """One schema-owned sequence serves all tables and drops after teardown."""
    await deploy_cow_functions(seeded_executor)
    await enable_cow_schema(seeded_executor)

    rows = await seeded_executor.execute(
        "SELECT pg_get_userbyid(seq.relowner), pg_get_userbyid(ns.nspowner), "
        "obj_description(seq.oid, 'pg_class') "
        "FROM pg_class seq "
        "JOIN pg_namespace ns ON ns.oid = seq.relnamespace "
        "WHERE ns.nspname = 'public' "
        "AND seq.relname = '_cow_operation_order_seq' "
        "AND seq.relkind = 'S'"
    )
    assert len(rows) == 1
    assert rows[0][0] == rows[0][1]
    assert rows[0][2] == "agent-cow deterministic operation order"

    await disable_cow(seeded_executor, "users")
    assert await seeded_executor.execute(
        "SELECT to_regclass('public._cow_operation_order_seq') IS NOT NULL"
    ) == [(True,)]

    await disable_cow_schema(seeded_executor)
    assert await seeded_executor.execute(
        "SELECT to_regclass('public._cow_operation_order_seq') IS NULL"
    ) == [(True,)]
