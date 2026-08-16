"""Regression coverage for the hardened PostgreSQL role boundary."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql

from agentcow.postgres import (
    apply_cow_variables,
    commit_cow_session,
    deploy_cow_functions,
    discard_cow_session,
    enable_cow,
    get_dirty_tables,
    get_operation_dependencies,
    get_session_operations,
    harden_cow_schema,
    reset_cow_variables,
    validate_cow_schema_privileges,
)

from conftest import PG_HOST, PG_PASSWORD, PG_PORT, PsycopgExecutor


@dataclass
class HardenedEnvironment:
    schema: str
    setup_role: str
    runtime_role: str
    reviewer_role: str
    outsider_role: str
    unsafe_role: str
    setup: psycopg.Connection
    runtime: psycopg.Connection
    reviewer: psycopg.Connection
    outsider: psycopg.Connection

    @property
    def setup_executor(self) -> PsycopgExecutor:
        return PsycopgExecutor(self.setup)

    @property
    def reviewer_executor(self) -> PsycopgExecutor:
        return PsycopgExecutor(self.reviewer)


def _connect(database: str, role: str) -> psycopg.Connection:
    return psycopg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=role,
        password=PG_PASSWORD,
        dbname=database,
    )


@asynccontextmanager
async def _hardened_environment(postgresql):
    token = uuid.uuid4().hex[:8]
    schema = f"content_{token}"
    setup_role = f"cow_setup_{token}"
    # The runtime role deliberately requires identifier quoting.
    runtime_role = f'COW Runtime "{token}'
    reviewer_role = f"cow_reviewer_{token}"
    outsider_role = f"cow_outsider_{token}"
    unsafe_role = f"cow_unsafe_{token}"
    login_roles = (setup_role, runtime_role, reviewer_role, outsider_role)
    all_roles = login_roles + (unsafe_role,)
    connections: list[psycopg.Connection] = []

    postgresql.rollback()
    with postgresql.cursor() as cursor:
        for role in login_roles:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOINHERIT"
                ).format(sql.Identifier(role), sql.Literal(PG_PASSWORD))
            )
        cursor.execute(
            sql.SQL(
                "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
            ).format(sql.Identifier(unsafe_role))
        )
        cursor.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(postgresql.info.dbname), sql.Identifier(setup_role)
            )
        )
    postgresql.commit()

    try:
        setup = _connect(postgresql.info.dbname, setup_role)
        connections.append(setup)
        setup_executor = PsycopgExecutor(setup)
        await setup_executor.execute(f'CREATE SCHEMA "{schema}"')
        await setup_executor.execute(
            f'CREATE TABLE "{schema}".items ('
            "id integer PRIMARY KEY, value text NOT NULL)"
        )
        await setup_executor.execute(
            f'INSERT INTO "{schema}".items VALUES '
            "(1, 'one'), (2, 'two'), (3, 'three')"
        )
        # This grant follows the original table when enable_cow renames it.
        await setup_executor.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {}.items TO {}")
            .format(sql.Identifier(schema), sql.Identifier(runtime_role))
            .as_string(setup)
        )
        await deploy_cow_functions(setup_executor)
        await enable_cow(setup_executor, "items", schema=schema)
        validation = await harden_cow_schema(
            setup_executor,
            schema=schema,
            runtime_roles=[runtime_role],
            reviewer_roles=[reviewer_role],
        )
        assert validation == {"safe": True, "violations": []}
        setup.commit()

        runtime = _connect(postgresql.info.dbname, runtime_role)
        reviewer = _connect(postgresql.info.dbname, reviewer_role)
        outsider = _connect(postgresql.info.dbname, outsider_role)
        connections.extend((runtime, reviewer, outsider))
        yield HardenedEnvironment(
            schema=schema,
            setup_role=setup_role,
            runtime_role=runtime_role,
            reviewer_role=reviewer_role,
            outsider_role=outsider_role,
            unsafe_role=unsafe_role,
            setup=setup,
            runtime=runtime,
            reviewer=reviewer,
            outsider=outsider,
        )
    finally:
        for connection in reversed(connections):
            connection.close()
        postgresql.rollback()
        with postgresql.cursor() as cursor:
            for role in all_roles:
                cursor.execute(
                    sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
                )
            for role in all_roles:
                cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        postgresql.commit()


def _assert_insufficient(connection: psycopg.Connection, statement: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute(statement)
    connection.rollback()


@pytest.mark.asyncio
async def test_canonical_write_compatibility_requires_explicit_opt_in(
    seeded_executor,
):
    """The downstream default fails closed; legacy write-through is explicit."""
    await deploy_cow_functions(seeded_executor)
    await enable_cow(seeded_executor, "users")
    seeded_executor.commit()

    with pytest.raises(psycopg.errors.InvalidParameterValue):
        await seeded_executor.execute(
            "INSERT INTO users (name, email) VALUES ('Blocked', 'blocked@test')"
        )
    seeded_executor._conn.rollback()
    assert await seeded_executor.execute(
        "SELECT count(*) FROM users_base WHERE email = 'blocked@test'"
    ) == [(0,)]

    await enable_cow(
        seeded_executor,
        "users",
        allow_unsafe_canonical_writes=True,
    )
    await seeded_executor.execute(
        "INSERT INTO users (name, email) VALUES ('Compatible', 'compatible@test')"
    )
    assert await seeded_executor.execute(
        "SELECT count(*) FROM users_base WHERE email = 'compatible@test'"
    ) == [(1,)]


@pytest.mark.asyncio
async def test_runtime_crud_is_isolated_and_ordered(postgresql):
    """A quoted runtime role needs only view CRUD and transaction context."""
    async with _hardened_environment(postgresql) as env:
        setup_identity = env.setup.execute(
            "SELECT current_user::text, rolsuper FROM pg_roles "
            "WHERE rolname = current_user"
        ).fetchone()
        assert setup_identity == (env.setup_role, False)

        session_id = uuid.uuid4()
        runtime_executor = PsycopgExecutor(env.runtime)
        with env.runtime.transaction():
            first_operation = uuid.uuid4()
            await apply_cow_variables(runtime_executor, session_id, first_operation)
            await runtime_executor.execute(
                f"INSERT INTO \"{env.schema}\".items VALUES (100, 'inserted')"
            )
            second_operation = uuid.uuid4()
            await apply_cow_variables(runtime_executor, session_id, second_operation)
            await runtime_executor.execute(
                f"UPDATE \"{env.schema}\".items SET value = 'updated' WHERE id = 1"
            )
            third_operation = uuid.uuid4()
            await apply_cow_variables(runtime_executor, session_id, third_operation)
            await runtime_executor.execute(
                f'DELETE FROM "{env.schema}".items WHERE id = 2'
            )
            assert await runtime_executor.execute(
                f'SELECT id, value FROM "{env.schema}".items ORDER BY id'
            ) == [(1, "updated"), (3, "three"), (100, "inserted")]

        assert env.setup.execute(
            f'SELECT id, value FROM "{env.schema}".items_base ORDER BY id'
        ).fetchall() == [(1, "one"), (2, "two"), (3, "three")]
        orders = [
            row[0]
            for row in env.setup.execute(
                f'SELECT _cow_order FROM "{env.schema}".items_changes '
                f"WHERE session_id = '{session_id}'::uuid ORDER BY _cow_order"
            ).fetchall()
        ]
        assert len(orders) == 3
        assert orders == sorted(set(orders))

        operations = await get_session_operations(
            env.reviewer_executor, session_id, schema=env.schema
        )
        assert operations == [first_operation, second_operation, third_operation]
        assert await get_dirty_tables(
            env.reviewer_executor, session_id, schema=env.schema
        ) == ["items"]
        dependencies = await get_operation_dependencies(
            env.reviewer_executor, session_id, schema=env.schema
        )
        assert isinstance(dependencies, list)
        env.reviewer.rollback()

        await commit_cow_session(
            env.reviewer_executor, "items", session_id, schema=env.schema
        )
        env.reviewer.commit()
        assert env.setup.execute(
            f'SELECT id, value FROM "{env.schema}".items_base ORDER BY id'
        ).fetchall() == [(1, "updated"), (3, "three"), (100, "inserted")]

        validation = await validate_cow_schema_privileges(
            env.setup_executor,
            schema=env.schema,
            runtime_roles=[env.runtime_role],
            reviewer_roles=[env.reviewer_role],
        )
        assert validation == {"safe": True, "violations": []}


@pytest.mark.asyncio
async def test_runtime_writes_fail_closed_without_complete_context(postgresql):
    """Missing, reset, expired, and malformed context never writes base state."""
    async with _hardened_environment(postgresql) as env:
        insert = f"INSERT INTO \"{env.schema}\".items VALUES (200, 'blocked')"

        with pytest.raises(psycopg.errors.InvalidParameterValue):
            with env.runtime.transaction():
                env.runtime.execute(insert)

        with pytest.raises(psycopg.errors.InvalidParameterValue):
            with env.runtime.transaction():
                env.runtime.execute(f"SET LOCAL app.session_id = '{uuid.uuid4()}'")
                env.runtime.execute(insert)

        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            with env.runtime.transaction():
                env.runtime.execute("SET LOCAL app.session_id = 'not-a-uuid'")
                env.runtime.execute(f"SET LOCAL app.operation_id = '{uuid.uuid4()}'")
                env.runtime.execute(insert)

        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            with env.runtime.transaction():
                env.runtime.execute(f"SET LOCAL app.session_id = '{uuid.uuid4()}'")
                env.runtime.execute("SET LOCAL app.operation_id = 'not-a-uuid'")
                env.runtime.execute(insert)

        runtime_executor = PsycopgExecutor(env.runtime)
        with env.runtime.transaction():
            await apply_cow_variables(runtime_executor, uuid.uuid4(), uuid.uuid4())
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            with env.runtime.transaction():
                env.runtime.execute(insert)

        with pytest.raises(psycopg.errors.InvalidParameterValue):
            with env.runtime.transaction():
                await apply_cow_variables(runtime_executor, uuid.uuid4(), uuid.uuid4())
                await reset_cow_variables(runtime_executor)
                env.runtime.execute(insert)

        assert env.setup.execute(
            f'SELECT count(*) FROM "{env.schema}".items_base WHERE id = 200'
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_runtime_and_public_cannot_bypass_internal_boundary(postgresql):
    """Arbitrary SQL under runtime credentials cannot reach protected state."""
    async with _hardened_environment(postgresql) as env:
        objects_and_statements = (
            ("items_base", "SELECT * FROM {object}"),
            ("items_base", "INSERT INTO {object} VALUES (8, 'x')"),
            ("items_base", "UPDATE {object} SET value = 'x' WHERE id = 1"),
            ("items_base", "DELETE FROM {object} WHERE id = 1"),
            ("items_changes", "SELECT * FROM {object}"),
            (
                "items_changes",
                "INSERT INTO {object} (session_id, operation_id, id, value) "
                f"VALUES ('{uuid.uuid4()}', '{uuid.uuid4()}', 8, 'x')",
            ),
            ("items_changes", "UPDATE {object} SET value = 'x'"),
            ("items_changes", "DELETE FROM {object}"),
            ("cow_dirty_tables", "SELECT * FROM {object}"),
            (
                "cow_dirty_tables",
                "INSERT INTO {object} VALUES "
                f"('{env.schema}', '{uuid.uuid4()}', 'items')",
            ),
            (
                "cow_dirty_tables",
                "UPDATE {object} SET table_name = 'items'",
            ),
            ("cow_dirty_tables", "DELETE FROM {object}"),
        )
        for object_name, template in objects_and_statements:
            qualified = f'"{env.schema}"."{object_name}"'
            _assert_insufficient(env.runtime, template.format(object=qualified))

        _assert_insufficient(
            env.runtime,
            f"SELECT nextval('{env.schema}._cow_operation_order_seq')",
        )
        _assert_insufficient(
            env.runtime,
            f'SELECT last_value FROM "{env.schema}"._cow_operation_order_seq',
        )
        _assert_insufficient(
            env.runtime,
            f"SELECT setval('{env.schema}._cow_operation_order_seq', 999, true)",
        )
        _assert_insufficient(
            env.runtime,
            f'CREATE TABLE "{env.schema}".runtime_object (id integer)',
        )
        _assert_insufficient(
            env.runtime,
            "CREATE TABLE agentcow.runtime_object (id integer)",
        )
        _assert_insufficient(
            env.runtime,
            f'CREATE OR REPLACE FUNCTION "{env.schema}".items_cow_upsert() '
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        )

        session_literal = str(uuid.uuid4())
        management_calls = (
            "SELECT agentcow.setup_cow('x', 'x_base', 'x', ARRAY['id'])",
            f"SELECT agentcow.teardown_cow('{env.schema}', 'items')",
            f"SELECT agentcow.commit_cow('{env.schema}', 'items_base', "
            f"ARRAY['id'], '{session_literal}'::uuid, NULL::uuid[])",
            f"SELECT agentcow.discard_cow('{env.schema}', 'items_base', "
            f"'{session_literal}'::uuid, NULL::uuid[])",
        )
        for statement in management_calls:
            _assert_insufficient(env.runtime, statement)
            _assert_insufficient(env.outsider, statement)
        _assert_insufficient(
            env.reviewer,
            "SELECT * FROM agentcow.get_cow_dirty_tables("
            f"'public', '{uuid.uuid4()}'::uuid)",
        )


@pytest.mark.asyncio
async def test_reviewer_has_only_controlled_inspection_and_promotion(postgresql):
    """Reviewer APIs commit/discard without exposing internal table DML."""
    async with _hardened_environment(postgresql) as env:
        runtime_executor = PsycopgExecutor(env.runtime)
        session_id = uuid.uuid4()
        with env.runtime.transaction():
            await apply_cow_variables(runtime_executor, session_id, uuid.uuid4())
            await runtime_executor.execute(
                f"UPDATE \"{env.schema}\".items SET value = 'reviewed' WHERE id = 1"
            )

        with env.reviewer.transaction():
            env.reviewer.execute(f"SET LOCAL app.session_id = '{session_id}'")
            assert env.reviewer.execute(
                f'SELECT value FROM "{env.schema}".items WHERE id = 1'
            ).fetchone() == ("reviewed",)
        assert await get_session_operations(
            env.reviewer_executor, session_id, schema=env.schema
        )
        env.reviewer.rollback()

        for object_name in ("items_base", "items_changes", "cow_dirty_tables"):
            _assert_insufficient(
                env.reviewer,
                f'DELETE FROM "{env.schema}"."{object_name}"',
            )
            _assert_insufficient(
                env.reviewer,
                f'SELECT * FROM "{env.schema}"."{object_name}"',
            )
        _assert_insufficient(
            env.reviewer,
            f"INSERT INTO \"{env.schema}\".items VALUES (9, 'no reviewer DML')",
        )
        _assert_insufficient(
            env.reviewer,
            f"SELECT agentcow.setup_cow('{env.schema}', 'items_base', "
            "'items', ARRAY['id'])",
        )
        _assert_insufficient(
            env.reviewer,
            f"SELECT agentcow.teardown_cow('{env.schema}', 'items')",
        )
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            with env.reviewer.transaction():
                env.reviewer.execute(
                    f"SELECT agentcow.commit_cow('{env.schema}', 'items_base', "
                    f"ARRAY['value'], '{session_id}'::uuid, NULL::uuid[])"
                )

        await discard_cow_session(
            env.reviewer_executor, "items", session_id, schema=env.schema
        )
        env.reviewer.commit()
        assert env.setup.execute(
            f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
        ).fetchone() == ("one",)

        commit_session = uuid.uuid4()
        with env.runtime.transaction():
            await apply_cow_variables(runtime_executor, commit_session, uuid.uuid4())
            await runtime_executor.execute(
                f"UPDATE \"{env.schema}\".items SET value = 'committed' WHERE id = 1"
            )
        await commit_cow_session(
            env.reviewer_executor, "items", commit_session, schema=env.schema
        )
        env.reviewer.commit()
        assert env.setup.execute(
            f'SELECT value FROM "{env.schema}".items_base WHERE id = 1'
        ).fetchone() == ("committed",)


@pytest.mark.asyncio
async def test_inherited_unsafe_privilege_fails_effective_validation(postgresql):
    """Membership in a role with base access is reported, not papered over."""
    async with _hardened_environment(postgresql) as env:
        postgresql.rollback()
        with postgresql.cursor() as cursor:
            cursor.execute(
                sql.SQL("GRANT SELECT, UPDATE ON {}.items_base TO {}").format(
                    sql.Identifier(env.schema), sql.Identifier(env.unsafe_role)
                )
            )
            cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(env.unsafe_role), sql.Identifier(env.runtime_role)
                )
            )
        postgresql.commit()

        validation = await validate_cow_schema_privileges(
            env.setup_executor,
            schema=env.schema,
            runtime_roles=[env.runtime_role],
            reviewer_roles=[env.reviewer_role],
        )
        assert validation["safe"] is False
        assert any("items_base" in item for item in validation["violations"])

        with pytest.raises(RuntimeError, match="validation failed"):
            await harden_cow_schema(
                env.setup_executor,
                schema=env.schema,
                runtime_roles=[env.runtime_role],
                reviewer_roles=[env.reviewer_role],
            )
        env.setup.rollback()
