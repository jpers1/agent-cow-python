"""Release-candidate integration coverage for the hardened public path."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

import agentcow
import agentcow.postgres as postgres_api
from agentcow.postgres import (
    CowConflictError,
    asyncpg_cow_reviewer,
    asyncpg_cow_session,
    validate_cow_schema_privileges,
)

from test_role_hardening import _hardened_environment
from test_safe_promotion import _enable_tables, _pool


RECOMMENDED_PUBLIC_API = {
    "deploy_cow_functions",
    "enable_cow",
    "enable_cow_schema",
    "harden_cow_schema",
    "validate_cow_schema_privileges",
    "asyncpg_cow_session",
    "sqlalchemy_cow_session",
    "CowSession",
    "get_cow_conflicts",
    "asyncpg_cow_reviewer",
    "sqlalchemy_cow_reviewer",
    "CowReviewer",
    "CowConflictError",
}


def test_release_candidate_version_and_recommended_exports() -> None:
    """Release documentation names only real downstream public APIs."""
    assert agentcow.__version__ == "0.2.0rc1"
    assert RECOMMENDED_PUBLIC_API <= set(postgres_api.__all__)


@pytest.mark.asyncio
async def test_release_hardened_lifecycle_conflict_and_discard(postgresql) -> None:
    """Exercise the intended non-superuser runtime/reviewer lifecycle."""
    async with _hardened_environment(postgresql) as env:
        assert env.setup.execute(
            "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
        ).fetchone() == (False,)
        validation = await validate_cow_schema_privileges(
            env.setup_executor,
            schema=env.schema,
            runtime_roles=[env.runtime_role],
            reviewer_roles=[env.reviewer_role],
        )
        assert validation == {"safe": True, "violations": []}
        env.setup.rollback()

        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            accepted_session = uuid.uuid4()
            first_operation = uuid.uuid4()
            second_operation = uuid.uuid4()
            async with asyncpg_cow_session(
                runtime_pool,
                session_id=accepted_session,
                operation_id=first_operation,
            ) as cow:
                await cow.execute(
                    f'UPDATE "{env.schema}".items '
                    "SET value = 'draft-one' WHERE id = 1"
                )
                await cow.set_operation(second_operation)
                await cow.execute(
                    f'UPDATE "{env.schema}".items '
                    "SET value = 'draft-two' WHERE id = 1"
                )

            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                assert (
                    await reviewer.conflicts(accepted_session, schema=env.schema) == []
                )
                assert await reviewer.operations(
                    accepted_session, schema=env.schema
                ) == [first_operation, second_operation]
                accepted = await reviewer.commit_session(
                    accepted_session, schema=env.schema
                )
            assert accepted.committed_tables == ("items",)
            assert accepted.has_pending_operations is False
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("draft-two",)

            discarded_session = uuid.uuid4()
            async with asyncpg_cow_session(
                runtime_pool, session_id=discarded_session
            ) as cow:
                await cow.execute(
                    f'UPDATE "{env.schema}".items '
                    "SET value = 'discard-me' WHERE id = 1"
                )
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                discarded = await reviewer.discard_session(
                    discarded_session, schema=env.schema
                )
            assert discarded.discarded_tables == ("items",)
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("draft-two",)

            conflicting_session = uuid.uuid4()
            async with asyncpg_cow_session(
                runtime_pool, session_id=conflicting_session
            ) as cow:
                await cow.execute(
                    f'UPDATE "{env.schema}".items '
                    "SET value = 'pending-b' WHERE id = 1"
                )
            env.setup.execute(
                f'UPDATE "{env.schema}".items_base '
                "SET value = 'canonical-c' WHERE id = 1"
            )
            env.setup.commit()

            with pytest.raises(CowConflictError) as caught:
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.commit_session(
                        conflicting_session, schema=env.schema
                    )
            assert [item["conflict_kind"] for item in caught.value.conflicts] == [
                "BASE_ROW_CHANGED"
            ]
            assert env.setup.execute(
                f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
            ).fetchone() == ("canonical-c",)
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                assert await reviewer.operations(conflicting_session, schema=env.schema)
                await reviewer.discard_session(conflicting_session, schema=env.schema)
            assert env.setup.execute(
                f'SELECT count(*) FROM "{env.schema}".items_changes '
                f"WHERE session_id = '{conflicting_session}'::uuid"
            ).fetchone() == (0,)
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()


@pytest.mark.asyncio
async def test_release_multitable_failure_is_atomic_and_connection_reusable(
    postgresql,
) -> None:
    """A later table failure rolls back canonical mutation and keeps pending state."""
    async with _hardened_environment(postgresql) as env:
        await _enable_tables(
            env,
            [
                f'CREATE TABLE "{env.schema}".a_release_ok '
                "(id integer PRIMARY KEY, value text NOT NULL)",
                f'CREATE TABLE "{env.schema}".z_release_bad '
                "(id integer PRIMARY KEY, value integer CHECK (value > 0))",
            ],
            ["a_release_ok", "z_release_bad"],
        )
        runtime_pool = await _pool(env, env.runtime_role, max_size=1)
        reviewer_pool = await _pool(env, env.reviewer_role, max_size=1)
        try:
            session_id = uuid.uuid4()
            async with asyncpg_cow_session(runtime_pool, session_id=session_id) as cow:
                await cow.execute(
                    f"INSERT INTO \"{env.schema}\".a_release_ok VALUES (1, 'good')"
                )
                await cow.set_operation()
                await cow.execute(
                    f'INSERT INTO "{env.schema}".z_release_bad VALUES (1, -1)'
                )

            with pytest.raises(asyncpg.CheckViolationError):
                async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                    await reviewer.commit_session(session_id, schema=env.schema)

            assert (
                env.setup.execute(
                    f'SELECT * FROM "{env.schema}".a_release_ok_base'
                ).fetchall()
                == []
            )
            assert (
                env.setup.execute(
                    f'SELECT * FROM "{env.schema}".z_release_bad_base'
                ).fetchall()
                == []
            )
            for table in ("a_release_ok", "z_release_bad"):
                assert env.setup.execute(
                    f'SELECT count(*) FROM "{env.schema}".{table}_changes '
                    f"WHERE session_id = '{session_id}'::uuid"
                ).fetchone() == (1,)

            async with reviewer_pool.acquire() as connection:
                assert connection.is_in_transaction() is False
                assert await connection.fetchval("SELECT 1") == 1
            async with asyncpg_cow_reviewer(reviewer_pool) as reviewer:
                await reviewer.discard_session(session_id, schema=env.schema)
        finally:
            await runtime_pool.close()
            await reviewer_pool.close()
