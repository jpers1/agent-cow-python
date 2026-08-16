"""Regression coverage for search-path-independent PostgreSQL internals."""

from __future__ import annotations

import uuid

import pytest

from agentcow.postgres import (
    apply_cow_variables,
    commit_cow_session_schema,
    deploy_cow_functions,
    disable_cow,
    disable_cow_schema,
    discard_cow_session_schema,
    enable_cow,
    enable_cow_schema,
    get_dirty_tables,
    get_operation_dependencies,
    get_session_operations,
    reset_cow_variables,
)


async def _set_operation(executor, session_id, operation_id) -> None:
    await apply_cow_variables(executor, session_id, operation_id)


@pytest.mark.asyncio
async def test_pg_temp_registry_and_sequence_cannot_shadow_public_cow(
    seeded_executor,
):
    """Temp objects cannot intercept dirty tracking, promotion, or teardown."""
    await deploy_cow_functions(seeded_executor)
    await seeded_executor.execute(
        "CREATE TEMP TABLE cow_dirty_tables ("
        "schema_name text NOT NULL, session_id uuid NOT NULL, "
        "table_name text NOT NULL, "
        "PRIMARY KEY (schema_name, session_id, table_name))"
    )
    await seeded_executor.execute("CREATE TEMP SEQUENCE _cow_operation_order_seq")
    await seeded_executor.execute(
        "SELECT setval('pg_temp._cow_operation_order_seq', 900, true)"
    )
    await seeded_executor.execute("SET search_path = pg_temp, public")
    await enable_cow(seeded_executor, "users")

    commit_session = uuid.uuid4()
    await _set_operation(seeded_executor, commit_session, uuid.uuid4())
    await seeded_executor.execute(
        "INSERT INTO public.users (id, name, email) "
        "VALUES (100, 'Committed', 'committed@example.test')"
    )
    await reset_cow_variables(seeded_executor)

    assert await get_dirty_tables(seeded_executor, commit_session) == ["users"]
    assert await seeded_executor.execute(
        "SELECT count(*) FROM pg_temp.cow_dirty_tables"
    ) == [(0,)]
    assert await seeded_executor.execute(
        "SELECT table_name FROM public.cow_dirty_tables "
        f"WHERE session_id = '{commit_session}'::uuid"
    ) == [("users",)]
    assert await seeded_executor.execute(
        "SELECT last_value FROM pg_temp._cow_operation_order_seq"
    ) == [(900,)]

    assert await commit_cow_session_schema(seeded_executor, commit_session) == ["users"]
    assert await seeded_executor.execute(
        "SELECT name FROM public.users_base WHERE id = 100"
    ) == [("Committed",)]

    discard_session = uuid.uuid4()
    await _set_operation(seeded_executor, discard_session, uuid.uuid4())
    await seeded_executor.execute(
        "UPDATE public.users SET name = 'Discarded' WHERE id = 1"
    )
    await reset_cow_variables(seeded_executor)
    assert await discard_cow_session_schema(seeded_executor, discard_session) == [
        "users"
    ]
    assert await seeded_executor.execute(
        "SELECT name FROM public.users_base WHERE id = 1"
    ) == [("Bessie",)]

    teardown_session = uuid.uuid4()
    await _set_operation(seeded_executor, teardown_session, uuid.uuid4())
    await seeded_executor.execute("DELETE FROM public.users WHERE id = 2")
    await reset_cow_variables(seeded_executor)
    await seeded_executor.execute(
        "INSERT INTO pg_temp.cow_dirty_tables VALUES "
        f"('shadow', '{uuid.uuid4()}'::uuid, 'sentinel')"
    )
    await disable_cow(seeded_executor, "users")

    assert await seeded_executor.execute(
        "SELECT count(*) FROM public.cow_dirty_tables"
    ) == [(0,)]
    assert await seeded_executor.execute(
        "SELECT table_name FROM pg_temp.cow_dirty_tables"
    ) == [("sentinel",)]


@pytest.mark.asyncio
async def test_attacker_schema_functions_cannot_redirect_internal_calls(
    seeded_executor,
):
    """Same-named helpers ahead of public never receive internal calls."""
    await seeded_executor.execute("CREATE SCHEMA hostile")
    await seeded_executor.execute("""
        CREATE FUNCTION hostile._cow_changes_table_name(text)
        RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'redirected_changes' $$;

        CREATE FUNCTION hostile.setup_cow(text, text, text, text[])
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'hostile setup called'; END
        $$;

        CREATE FUNCTION hostile._cow_dirty_changes_tables(text, uuid)
        RETURNS TABLE(table_name text) LANGUAGE sql STABLE AS $$
        SELECT 'redirected_changes'::text
        $$;

        CREATE FUNCTION hostile.commit_cow(text, text, text[], uuid, uuid[])
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'hostile commit called'; END
        $$;

        CREATE FUNCTION hostile.discard_cow(text, text, uuid, uuid[])
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'hostile discard called'; END
        $$;

        CREATE FUNCTION hostile.teardown_cow(text, text)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'hostile teardown called'; END
        $$;

        CREATE FUNCTION hostile.get_cow_dependencies(text, uuid)
        RETURNS TABLE(depends_on uuid, operation_id uuid) LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'hostile dependencies called'; END
        $$;

        CREATE FUNCTION hostile.get_cow_session_operations(text, uuid)
        RETURNS TABLE(operation_id uuid, earliest_change timestamptz)
        LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'hostile operations called'; END
        $$;

        CREATE FUNCTION hostile._cow_fk_edges(text, text[])
        RETURNS TABLE(
            parent_base_table text,
            child_base_table text,
            is_self_ref boolean
        ) LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'hostile FK edges called'; END
        $$;
        """)
    await seeded_executor.execute("SET search_path = hostile, pg_temp, public")

    await deploy_cow_functions(seeded_executor)
    await enable_cow(seeded_executor, "users")

    session_id = uuid.uuid4()
    first_operation = uuid.uuid4()
    second_operation = uuid.uuid4()
    await _set_operation(seeded_executor, session_id, first_operation)
    await seeded_executor.execute("UPDATE public.users SET name = 'First' WHERE id = 1")
    await _set_operation(seeded_executor, session_id, second_operation)
    await seeded_executor.execute(
        "UPDATE public.users SET name = 'Second' WHERE id = 1"
    )

    dependencies = await get_operation_dependencies(seeded_executor, session_id)
    assert (first_operation, second_operation) in dependencies
    assert await get_session_operations(seeded_executor, session_id) == [
        first_operation,
        second_operation,
    ]
    await reset_cow_variables(seeded_executor)
    assert await commit_cow_session_schema(seeded_executor, session_id) == ["users"]
    assert await seeded_executor.execute(
        "SELECT name FROM public.users_base WHERE id = 1"
    ) == [("Second",)]

    discard_session = uuid.uuid4()
    await _set_operation(seeded_executor, discard_session, uuid.uuid4())
    await seeded_executor.execute(
        "UPDATE public.users SET name = 'Discarded' WHERE id = 1"
    )
    await reset_cow_variables(seeded_executor)
    assert await discard_cow_session_schema(seeded_executor, discard_session) == [
        "users"
    ]
    await disable_cow(seeded_executor, "users")

    function_configs = await seeded_executor.execute(
        "SELECT proc.proname, proc.proconfig "
        "FROM pg_catalog.pg_proc proc "
        "JOIN pg_catalog.pg_namespace ns ON ns.oid = proc.pronamespace "
        "WHERE ns.nspname = 'agentcow'"
    )
    assert function_configs
    assert all(
        "search_path=pg_catalog" in (config or []) for _, config in function_configs
    )


@pytest.mark.asyncio
async def test_non_public_schema_is_independent_of_search_path(seeded_executor):
    """All COW paths work when the application schema is never searched."""
    schema = "content_test"
    await seeded_executor.execute(f"CREATE SCHEMA {schema}")
    await seeded_executor.execute(f"""
        CREATE TABLE {schema}.authors (
            id integer PRIMARY KEY,
            name text NOT NULL
        );
        CREATE TABLE {schema}.articles (
            id integer PRIMARY KEY,
            author_id integer NOT NULL REFERENCES {schema}.authors(id),
            title text NOT NULL
        );
        INSERT INTO {schema}.authors VALUES (1, 'Canonical');
        INSERT INTO {schema}.articles VALUES (1, 1, 'Canonical article');
        """)
    await deploy_cow_functions(seeded_executor)
    await seeded_executor.execute("SET search_path = pg_temp, public")
    assert sorted(await enable_cow_schema(seeded_executor, schema=schema)) == [
        "articles",
        "authors",
    ]

    session_id = uuid.uuid4()
    insert_author = uuid.uuid4()
    update_author = uuid.uuid4()
    insert_article = uuid.uuid4()
    await _set_operation(seeded_executor, session_id, insert_author)
    await seeded_executor.execute(
        f"INSERT INTO {schema}.authors VALUES (10, 'First name')"
    )
    await _set_operation(seeded_executor, session_id, update_author)
    await seeded_executor.execute(
        f"UPDATE {schema}.authors SET name = 'Latest name' WHERE id = 10"
    )
    await _set_operation(seeded_executor, session_id, insert_article)
    await seeded_executor.execute(
        f"INSERT INTO {schema}.articles VALUES (10, 10, 'New article')"
    )

    author_changes = await seeded_executor.execute(
        f"SELECT operation_id, _cow_updated_at, _cow_order "
        f"FROM {schema}.authors_changes WHERE id = 10 ORDER BY _cow_order"
    )
    article_change = (
        await seeded_executor.execute(
            f"SELECT operation_id, _cow_updated_at, _cow_order "
            f"FROM {schema}.articles_changes WHERE id = 10"
        )
    )[0]
    assert author_changes[0][1] == author_changes[1][1] == article_change[1]
    assert author_changes[0][2] < author_changes[1][2] < article_change[2]
    assert await seeded_executor.execute(
        f"SELECT name FROM {schema}.authors WHERE id = 10"
    ) == [("Latest name",)]
    assert sorted(await get_dirty_tables(seeded_executor, session_id, schema)) == [
        "articles",
        "authors",
    ]

    dependencies = await get_operation_dependencies(seeded_executor, session_id, schema)
    assert (insert_author, update_author) in dependencies
    assert (insert_author, insert_article) in dependencies

    await reset_cow_variables(seeded_executor)
    committed = await commit_cow_session_schema(
        seeded_executor, session_id, schema=schema
    )
    assert committed == ["authors", "articles"]
    assert await seeded_executor.execute(
        f"SELECT name FROM {schema}.authors_base WHERE id = 10"
    ) == [("Latest name",)]
    assert await seeded_executor.execute(
        f"SELECT title FROM {schema}.articles_base WHERE id = 10"
    ) == [("New article",)]

    discard_session = uuid.uuid4()
    await _set_operation(seeded_executor, discard_session, uuid.uuid4())
    await seeded_executor.execute(
        f"UPDATE {schema}.authors SET name = 'Discard me' WHERE id = 10"
    )
    await _set_operation(seeded_executor, discard_session, uuid.uuid4())
    await seeded_executor.execute(f"DELETE FROM {schema}.articles WHERE id = 10")
    await reset_cow_variables(seeded_executor)
    assert sorted(
        await discard_cow_session_schema(
            seeded_executor, discard_session, schema=schema
        )
    ) == ["articles", "authors"]
    assert await seeded_executor.execute(
        f"SELECT name FROM {schema}.authors_base WHERE id = 10"
    ) == [("Latest name",)]
    assert await seeded_executor.execute(
        f"SELECT title FROM {schema}.articles_base WHERE id = 10"
    ) == [("New article",)]

    teardown_session = uuid.uuid4()
    await _set_operation(seeded_executor, teardown_session, uuid.uuid4())
    await seeded_executor.execute(f"DELETE FROM {schema}.articles WHERE id = 10")
    await reset_cow_variables(seeded_executor)
    assert sorted(await disable_cow_schema(seeded_executor, schema=schema)) == [
        "articles",
        "authors",
    ]
    assert await seeded_executor.execute(
        f"SELECT count(*) FROM {schema}.cow_dirty_tables"
    ) == [(0,)]
    assert await seeded_executor.execute(
        "SELECT to_regclass('content_test._cow_operation_order_seq') IS NULL"
    ) == [(True,)]
    assert await seeded_executor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'content_test' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name"
    ) == [("articles",), ("authors",), ("cow_dirty_tables",)]


@pytest.mark.asyncio
async def test_h01_non_public_dirty_registry_is_migrated_without_losing_changes(
    seeded_executor,
):
    """H02 moves H01 registry metadata while preserving pending rows."""
    schema = "legacy_content"
    session_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    await seeded_executor.execute(f"""
        CREATE SCHEMA {schema};
        CREATE TABLE {schema}.notes_base (
            id integer PRIMARY KEY,
            body text NOT NULL
        );
        INSERT INTO {schema}.notes_base VALUES (1, 'Canonical');
        CREATE TABLE {schema}.notes_changes (
            session_id uuid NOT NULL,
            operation_id uuid NOT NULL,
            id integer NOT NULL,
            body text NOT NULL,
            _cow_deleted boolean NOT NULL DEFAULT false,
            _cow_updated_at timestamptz NOT NULL DEFAULT now(),
            _cow_order bigint NOT NULL,
            PRIMARY KEY (session_id, operation_id, id)
        );
        INSERT INTO {schema}.notes_changes
            (session_id, operation_id, id, body, _cow_order)
        VALUES
            ('{session_id}'::uuid, '{operation_id}'::uuid, 1, 'Pending', 5);
        CREATE VIEW {schema}.notes AS
            SELECT id, body FROM {schema}.notes_base;
        CREATE TABLE public.cow_dirty_tables (
            schema_name text NOT NULL,
            session_id uuid NOT NULL,
            table_name text NOT NULL,
            PRIMARY KEY (schema_name, session_id, table_name)
        );
        INSERT INTO public.cow_dirty_tables
        VALUES ('{schema}', '{session_id}'::uuid, 'notes');
        """)

    await deploy_cow_functions(seeded_executor)

    assert await get_dirty_tables(seeded_executor, session_id, schema) == ["notes"]
    assert await seeded_executor.execute(
        f"SELECT table_name FROM {schema}.cow_dirty_tables "
        f"WHERE session_id = '{session_id}'::uuid"
    ) == [("notes",)]
    assert await seeded_executor.execute(
        "SELECT count(*) FROM public.cow_dirty_tables"
    ) == [(0,)]
    assert await commit_cow_session_schema(
        seeded_executor, session_id, schema=schema
    ) == ["notes"]
    assert await seeded_executor.execute(
        f"SELECT body FROM {schema}.notes_base WHERE id = 1"
    ) == [("Pending",)]
