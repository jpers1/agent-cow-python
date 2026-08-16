"""
Core COW functionality for PostgreSQL.

Provides high-level async functions for managing Copy-On-Write tables.
All functions accept a generic ``Executor`` — no ORM or driver dependency.
"""

import uuid
from contextlib import asynccontextmanager
from graphlib import CycleError, TopologicalSorter
from typing import Any, AsyncIterator, Protocol, TypedDict, runtime_checkable

from .cow_sql_functions import (
    COW_CHANGES_TABLE_NAME_SQL,
    CREATE_INTERNAL_SCHEMA_SQL,
    DROP_LEGACY_SETUP_FUNCTION_SQL,
    CREATE_HARDENED_ROLES_TABLE_SQL,
    CREATE_TABLE_SECURITY_MODES_SQL,
    REVOKE_PUBLIC_CONTROL_SCHEMA_SQL,
    REVOKE_PUBLIC_CONTROL_TABLES_SQL,
    REVOKE_PUBLIC_CONTROL_FUNCTIONS_SQL,
    REQUIRE_REVIEWER_SQL,
    REQUIRE_COW_TABLE_SQL,
    REQUIRE_PRIMARY_KEY_SQL,
    SETUP_COW_SQL,
    COMMIT_COW_UPSERT_SQL,
    COMMIT_COW_DELETE_SQL,
    COMMIT_COW_CLEANUP_SQL,
    COMMIT_COW_SQL,
    DISCARD_COW_SQL,
    TEARDOWN_COW_SQL,
    GET_DIRTY_CHANGES_TABLES_SQL,
    GET_COW_DEPENDENCIES_SQL,
    GET_SESSION_OPERATIONS_SQL,
    GET_COW_DIRTY_TABLES_SQL,
    GET_COW_PRIMARY_KEY_COLUMNS_SQL,
    GET_COW_FK_EDGES_SQL,
)
from .operations import (
    COW_FUNCTION_NAMES,
    setup_cow_sql,
    teardown_cow_sql,
    rename_table_sql,
    check_cow_state_sql,
    check_cow_disable_state_sql,
    check_table_is_base_table_sql,
    get_table_pk_cols_sql,
    check_cow_functions_deployed_sql,
    list_user_tables_sql,
    list_base_tables_sql,
    list_changes_tables_sql,
    list_enabled_cow_tables_sql,
    check_table_has_any_rows_sql,
    commit_cow_session_sql,
    discard_cow_session_sql,
    commit_cow_operations_sql,
    discard_cow_operations_sql,
    commit_cow_upsert_sql,
    commit_cow_delete_sql,
    commit_cow_cleanup_sql,
    get_cow_fk_edges_sql,
    alter_fk_constraints_deferrable_sql,
    alter_fk_constraints_not_deferrable_sql,
    get_session_operations_sql,
    get_operation_dependencies_sql,
    set_visible_operations_sql,
    get_dirty_tables_sql,
    _quote_ident,
    _quote_literal,
)
from .context import CowPostgresConfig, build_cow_variable_statements


@runtime_checkable
class Executor(Protocol):
    """Minimal async SQL executor.

    Any object with an ``execute`` method that accepts a SQL string and
    returns rows as ``list[tuple[Any, ...]]`` satisfies this protocol.

    Example adapters::

        # SQLAlchemy AsyncSession
        class SAExecutor:
            def __init__(self, session):
                self._s = session
            async def execute(self, sql):
                from sqlalchemy import text
                r = await self._s.execute(text(sql))
                return [tuple(row) for row in r.fetchall()] if r.returns_rows else []

        # asyncpg Connection
        class PGExecutor:
            def __init__(self, conn):
                self._c = conn
            async def execute(self, sql):
                return [tuple(r) for r in await self._c.fetch(sql)]
    """

    async def execute(self, sql: str) -> list[tuple[Any, ...]]: ...


class CowStatus(TypedDict):
    enabled: bool
    tables_with_cow: list[str]
    changes_tables: list[str]
    cow_functions_deployed: bool


class CowPrivilegeValidation(TypedDict):
    safe: bool
    violations: list[str]


_REVIEWER_FUNCTIONS = (
    ("get_cow_dirty_tables", "text, uuid"),
    ("get_cow_primary_key_columns", "text, text"),
    ("get_cow_session_operations", "text, uuid"),
    ("get_cow_dependencies", "text, uuid"),
    ("_cow_fk_edges", "text, text[]"),
    ("commit_cow", "text, text, text[], uuid, uuid[]"),
    ("commit_cow_upsert", "text, text, text[], uuid, uuid[]"),
    ("commit_cow_delete", "text, text, text[], uuid, uuid[]"),
    ("commit_cow_cleanup", "text, text, uuid, uuid[]"),
    ("discard_cow", "text, text, uuid, uuid[]"),
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_pk_cols(executor: Executor, schema: str, table_name: str) -> list[str]:
    """Resolve primary-key columns for a table by querying the database."""
    rows = await executor.execute(get_table_pk_cols_sql(schema, table_name))
    pk_cols = [row[0] for row in rows]
    if not pk_cols:
        raise ValueError(f"Table {table_name} in schema {schema} has no primary key.")
    return pk_cols


@asynccontextmanager
async def deferred_fk_constraints(executor: Executor) -> AsyncIterator[None]:
    """Defer FK constraint checks for the duration of the block.

    Use this around multi-table commit loops so that cross-table FK
    references are validated only after all tables have been committed.
    Requires FK constraints to be ``DEFERRABLE INITIALLY IMMEDIATE``
    (see :func:`enable_cow` / :func:`enable_cow_schema` with
    ``allow_deferred_fks=True``).
    """
    await executor.execute("SET CONSTRAINTS ALL DEFERRED")
    yield
    await executor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _topologically_sort_tables(
    tables: list[str],
    edges: list[tuple[str, str]],
) -> list[str]:
    """Return *tables* ordered so that parents come before children.

    Raises :class:`ValueError` with actionable remediation if the FK
    subgraph contains a cycle.
    """
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for table in tables:
        sorter.add(table)
    for parent, child in edges:
        if parent != child:
            sorter.add(child, parent)
    try:
        return list(sorter.static_order())
    except CycleError as exc:
        cycle = list(dict.fromkeys(exc.args[1]))
        raise ValueError(
            f"Cycle detected among tables {cycle}; commit_cow_session_schema "
            "cannot order inserts/deletes automatically. Either break the cycle "
            "in your schema or pass defer_fk_constraints=True (requires FKs to "
            "be DEFERRABLE; see enable_cow(allow_deferred_fks=True))."
        ) from exc


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


async def deploy_cow_functions(executor: Executor) -> None:
    """Deploy the required PL/pgSQL helper functions to the database.

    Existing upstream-format COW tables are upgraded only when their changes
    tables are empty. Pending rows cannot be assigned a truthful historical
    order, so deployment fails before replacing any function in that case.
    """
    enabled_tables: list[tuple[str, str, str, list[str]]] = []
    for (
        schema,
        view_name,
        base_table,
        changes_table,
        has_order,
    ) in await executor.execute(list_enabled_cow_tables_sql()):
        if not has_order:
            rows = await executor.execute(
                check_table_has_any_rows_sql(schema, changes_table)
            )
            if rows and rows[0][0]:
                raise RuntimeError(
                    "Cannot upgrade deterministic COW ordering for "
                    f"{schema}.{view_name}: {schema}.{changes_table} contains "
                    "pending legacy changes. Commit or discard them with the "
                    "previous agent-cow version before deploying this version."
                )
        pk_cols = await _get_pk_cols(executor, schema, base_table)
        enabled_tables.append((schema, view_name, base_table, pk_cols))

    for sql in (
        CREATE_INTERNAL_SCHEMA_SQL,
        REVOKE_PUBLIC_CONTROL_SCHEMA_SQL,
        CREATE_HARDENED_ROLES_TABLE_SQL,
        CREATE_TABLE_SECURITY_MODES_SQL,
        REVOKE_PUBLIC_CONTROL_TABLES_SQL,
        COW_CHANGES_TABLE_NAME_SQL,
        REQUIRE_REVIEWER_SQL,
        REQUIRE_COW_TABLE_SQL,
        REQUIRE_PRIMARY_KEY_SQL,
        DROP_LEGACY_SETUP_FUNCTION_SQL,
        SETUP_COW_SQL,
        COMMIT_COW_UPSERT_SQL,
        COMMIT_COW_DELETE_SQL,
        COMMIT_COW_CLEANUP_SQL,
        COMMIT_COW_SQL,
        DISCARD_COW_SQL,
        TEARDOWN_COW_SQL,
        GET_DIRTY_CHANGES_TABLES_SQL,
        GET_COW_DEPENDENCIES_SQL,
        GET_SESSION_OPERATIONS_SQL,
        GET_COW_DIRTY_TABLES_SQL,
        GET_COW_PRIMARY_KEY_COLUMNS_SQL,
        GET_COW_FK_EDGES_SQL,
        REVOKE_PUBLIC_CONTROL_FUNCTIONS_SQL,
    ):
        await executor.execute(sql)

    for schema, view_name, base_table, pk_cols in enabled_tables:
        await executor.execute(setup_cow_sql(schema, base_table, view_name, pk_cols))


# ---------------------------------------------------------------------------
# Role and privilege hardening
# ---------------------------------------------------------------------------


async def _resolve_roles(
    executor: Executor,
    role_names: list[str],
) -> dict[str, int]:
    unique_names = list(dict.fromkeys(role_names))
    if not unique_names:
        return {}
    role_literals = ", ".join(_quote_literal(name) for name in unique_names)
    rows = await executor.execute(
        "SELECT rolname::text, oid::bigint FROM pg_catalog.pg_roles "
        f"WHERE rolname IN ({role_literals})"
    )
    roles = {name: int(oid) for name, oid in rows}
    missing = [name for name in unique_names if name not in roles]
    if missing:
        raise ValueError(f"PostgreSQL roles do not exist: {missing}")
    return roles


async def _enabled_tables_for_schema(
    executor: Executor,
    schema: str,
) -> list[tuple[str, str, list[str]]]:
    enabled: list[tuple[str, str, list[str]]] = []
    for (
        row_schema,
        view_name,
        base_table,
        _changes_table,
        _has_order,
    ) in await executor.execute(list_enabled_cow_tables_sql()):
        if row_schema == schema:
            enabled.append(
                (
                    view_name,
                    base_table,
                    await _get_pk_cols(executor, schema, base_table),
                )
            )
    return enabled


async def _assert_hardening_owner(
    executor: Executor,
    schema: str,
    enabled_tables: list[tuple[str, str, list[str]]],
) -> tuple[str, int]:
    identity_rows = await executor.execute(
        "SELECT current_user::text, session_user::text, "
        "(SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), "
        "(SELECT oid::bigint FROM pg_catalog.pg_roles WHERE rolname = current_user)"
    )
    current_user, _session_user, _is_superuser, current_oid = identity_rows[0]
    owner_rows = await executor.execute(
        "SELECT namespace_.nspname::text, "
        "pg_get_userbyid(namespace_.nspowner)::text "
        "FROM pg_catalog.pg_namespace namespace_ "
        f"WHERE namespace_.nspname IN ({_quote_literal('agentcow')}, "
        f"{_quote_literal(schema)})"
    )
    owners = dict(owner_rows)
    if owners.get("agentcow") != current_user:
        raise PermissionError(
            f"{current_user} must own schema 'agentcow' before applying COW "
            "hardening"
        )
    application_access = await executor.execute(
        "SELECT "
        f"has_schema_privilege(current_user, {_quote_literal(schema)}, 'USAGE'), "
        f"has_schema_privilege(current_user, {_quote_literal(schema)}, 'CREATE')"
    )
    if not application_access or not all(application_access[0]):
        raise PermissionError(
            f"{current_user} must have USAGE and CREATE on application schema "
            f"{schema!r} before applying COW hardening"
        )

    base_names = [base for _view, base, _pk in enabled_tables]
    if base_names:
        base_literals = ", ".join(_quote_literal(name) for name in base_names)
        base_owner_rows = await executor.execute(
            "SELECT table_.relname::text, "
            "pg_get_userbyid(table_.relowner)::text "
            "FROM pg_catalog.pg_class table_ "
            "JOIN pg_catalog.pg_namespace namespace_ "
            "ON namespace_.oid = table_.relnamespace "
            f"WHERE namespace_.nspname = {_quote_literal(schema)} "
            f"AND table_.relname IN ({base_literals})"
        )
        wrong_owners = [
            f"{schema}.{name} owned by {owner}"
            for name, owner in base_owner_rows
            if owner != current_user
        ]
        if wrong_owners:
            raise PermissionError(
                "The setup role must own every enabled base table: "
                + ", ".join(wrong_owners)
            )
    return current_user, int(current_oid)


def _function_reference(name: str, identity_arguments: str) -> str:
    qualified_name = f'{_quote_ident("agentcow")}.{_quote_ident(name)}'
    return f"{qualified_name}({identity_arguments})"


async def harden_cow_schema(
    executor: Executor,
    schema: str,
    runtime_roles: list[str],
    reviewer_roles: list[str],
) -> CowPrivilegeValidation:
    """Apply the downstream setup/runtime/reviewer privilege boundary.

    The caller must be the setup/owner role that owns the control schema and
    enabled base tables and has ``USAGE``/``CREATE`` on the application schema.
    A superuser is not required. Run this call in an explicit transaction so a
    failed effective-privilege validation can be rolled back atomically.
    """
    if not runtime_roles:
        raise ValueError("At least one runtime role is required")
    if not reviewer_roles:
        raise ValueError("At least one reviewer role is required")
    overlap = sorted(set(runtime_roles) & set(reviewer_roles))
    if overlap:
        raise ValueError(f"Roles cannot be both runtime and reviewer: {overlap}")

    func_rows = await executor.execute(check_cow_functions_deployed_sql())
    if not func_rows or func_rows[0][0] != len(COW_FUNCTION_NAMES):
        raise RuntimeError("Deploy COW functions before applying role hardening")

    enabled_tables = await _enabled_tables_for_schema(executor, schema)
    if not enabled_tables:
        raise ValueError(f"Schema {schema!r} has no COW-enabled tables")

    all_role_names = list(dict.fromkeys(runtime_roles + reviewer_roles))
    roles = await _resolve_roles(executor, all_role_names)
    setup_name, setup_oid = await _assert_hardening_owner(
        executor, schema, enabled_tables
    )
    if setup_name in roles:
        raise ValueError("The setup owner cannot also be a runtime or reviewer role")

    await executor.execute(
        "DELETE FROM agentcow._cow_hardened_roles "
        f"WHERE schema_name = {_quote_literal(schema)}"
    )
    role_rows = [(setup_name, setup_oid, "owner")]
    role_rows.extend((name, roles[name], "runtime") for name in runtime_roles)
    role_rows.extend((name, roles[name], "reviewer") for name in reviewer_roles)
    for role_name, role_oid, role_kind in role_rows:
        await executor.execute(
            "INSERT INTO agentcow._cow_hardened_roles "
            "(schema_name, role_oid, role_name, role_kind) VALUES ("
            f"{_quote_literal(schema)}, {role_oid}::oid, "
            f"{_quote_literal(role_name)}::name, {_quote_literal(role_kind)})"
        )

    # Control schema access is deny-by-default. Reviewer functions are granted
    # explicitly below; runtime CRUD never calls a control function directly.
    for sql in (
        REVOKE_PUBLIC_CONTROL_SCHEMA_SQL,
        REVOKE_PUBLIC_CONTROL_TABLES_SQL,
        REVOKE_PUBLIC_CONTROL_FUNCTIONS_SQL,
    ):
        await executor.execute(sql)

    for role_name in all_role_names:
        role_ident = _quote_ident(role_name)
        await executor.execute(f"REVOKE ALL ON SCHEMA agentcow FROM {role_ident}")
        await executor.execute(
            f"REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA agentcow FROM {role_ident}"
        )
        await executor.execute(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA agentcow FROM {role_ident}"
        )
        await executor.execute(
            f"GRANT USAGE ON SCHEMA {_quote_ident(schema)} TO {role_ident}"
        )
        await executor.execute(
            f"REVOKE CREATE ON SCHEMA {_quote_ident(schema)} FROM {role_ident}"
        )

    for reviewer in reviewer_roles:
        reviewer_ident = _quote_ident(reviewer)
        await executor.execute(f"GRANT USAGE ON SCHEMA agentcow TO {reviewer_ident}")
        for function_name, identity_args in _REVIEWER_FUNCTIONS:
            await executor.execute(
                f"GRANT EXECUTE ON FUNCTION "
                f"{_function_reference(function_name, identity_args)} "
                f"TO {reviewer_ident}"
            )

    for view_name, base_table, pk_cols in enabled_tables:
        changes_table = f"{view_name}_changes"
        await executor.execute(
            setup_cow_sql(
                schema,
                base_table,
                view_name,
                pk_cols,
                fail_closed_writes=True,
                security_definer_triggers=True,
            )
        )

        for role_name in all_role_names:
            role_ident = _quote_ident(role_name)
            for relation_name in (
                base_table,
                changes_table,
                "cow_dirty_tables",
            ):
                await executor.execute(
                    f"REVOKE ALL ON TABLE {_quote_ident(schema)}."
                    f"{_quote_ident(relation_name)} FROM {role_ident}"
                )
            await executor.execute(
                f"REVOKE ALL ON SEQUENCE {_quote_ident(schema)}."
                f"{_quote_ident('_cow_operation_order_seq')} FROM {role_ident}"
            )
            await executor.execute(
                f"REVOKE ALL ON TABLE {_quote_ident(schema)}."
                f"{_quote_ident(view_name)} FROM {role_ident}"
            )
            for suffix in ("_cow_upsert", "_cow_delete"):
                await executor.execute(
                    f"REVOKE ALL ON FUNCTION {_quote_ident(schema)}."
                    f"{_quote_ident(view_name + suffix)}() FROM {role_ident}"
                )

        for runtime in runtime_roles:
            await executor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON "
                f"{_quote_ident(schema)}.{_quote_ident(view_name)} "
                f"TO {_quote_ident(runtime)}"
            )
        for reviewer in reviewer_roles:
            await executor.execute(
                f"GRANT SELECT ON {_quote_ident(schema)}."
                f"{_quote_ident(view_name)} TO {_quote_ident(reviewer)}"
            )

        for internal_name, object_kind in (
            (changes_table, "TABLE"),
            ("cow_dirty_tables", "TABLE"),
            ("_cow_operation_order_seq", "SEQUENCE"),
        ):
            await executor.execute(
                f"REVOKE ALL ON {object_kind} {_quote_ident(schema)}."
                f"{_quote_ident(internal_name)} FROM PUBLIC"
            )
        await executor.execute(
            f"REVOKE ALL ON TABLE {_quote_ident(schema)}."
            f"{_quote_ident(view_name)} FROM PUBLIC"
        )

    validation = await validate_cow_schema_privileges(
        executor,
        schema=schema,
        runtime_roles=runtime_roles,
        reviewer_roles=reviewer_roles,
    )
    if not validation["safe"]:
        raise RuntimeError(
            "COW privilege hardening validation failed: "
            + "; ".join(validation["violations"])
        )
    return validation


async def validate_cow_schema_privileges(
    executor: Executor,
    schema: str,
    runtime_roles: list[str],
    reviewer_roles: list[str],
) -> CowPrivilegeValidation:
    """Validate effective (including inherited) privileges for a COW schema."""
    roles = await _resolve_roles(
        executor, list(dict.fromkeys(runtime_roles + reviewer_roles))
    )
    enabled_tables = await _enabled_tables_for_schema(executor, schema)
    violations: list[str] = []

    public_rows = await executor.execute(
        "SELECT "
        "EXISTS (SELECT 1 FROM aclexplode(COALESCE(namespace_.nspacl, "
        "acldefault('n', namespace_.nspowner))) acl "
        "WHERE acl.grantee = 0 AND acl.privilege_type IN ('USAGE', 'CREATE')) "
        "FROM pg_catalog.pg_namespace namespace_ "
        "WHERE namespace_.nspname = 'agentcow'"
    )
    if public_rows and public_rows[0][0]:
        violations.append("PUBLIC retains access to the agentcow control schema")

    public_function_rows = await executor.execute(
        "SELECT proc.proname::text "
        "FROM pg_catalog.pg_proc proc "
        "JOIN pg_catalog.pg_namespace namespace_ "
        "ON namespace_.oid = proc.pronamespace "
        "CROSS JOIN LATERAL aclexplode(COALESCE(proc.proacl, "
        "acldefault('f', proc.proowner))) acl "
        "WHERE namespace_.nspname = 'agentcow' "
        "AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'"
    )
    if public_function_rows:
        violations.append(
            "PUBLIC can execute control functions: "
            + ", ".join(sorted({row[0] for row in public_function_rows}))
        )

    function_rows = await executor.execute(
        "SELECT proc.oid::bigint, proc.proname::text, proc.prosecdef, "
        "COALESCE(array_to_string(proc.proconfig, ','), '') "
        "FROM pg_catalog.pg_proc proc "
        "JOIN pg_catalog.pg_namespace namespace_ "
        "ON namespace_.oid = proc.pronamespace "
        "WHERE namespace_.nspname = 'agentcow'"
    )
    reviewer_function_names = {name for name, _args in _REVIEWER_FUNCTIONS}

    for role_kind, role_names in (
        ("runtime", runtime_roles),
        ("reviewer", reviewer_roles),
    ):
        for role_name in role_names:
            role_oid = roles[role_name]
            schema_rows = await executor.execute(
                "SELECT "
                "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                f"AND has_schema_privilege(reachable.oid, "
                f"{_quote_literal(schema)}, 'CREATE')), "
                "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                "AND has_schema_privilege(reachable.oid, 'agentcow', 'CREATE'))"
            )
            if schema_rows[0][0]:
                violations.append(
                    f"{role_kind} role {role_name!r} can CREATE in schema {schema!r}"
                )
            if schema_rows[0][1]:
                violations.append(
                    f"{role_kind} role {role_name!r} can CREATE in control schema"
                )

            for (
                function_oid,
                function_name,
                security_definer,
                proconfig,
            ) in function_rows:
                privilege_rows = await executor.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    f"AND has_function_privilege(reachable.oid, "
                    f"{function_oid}::oid, 'EXECUTE'))"
                )
                can_execute = bool(privilege_rows[0][0])
                if role_kind == "runtime" and can_execute:
                    violations.append(
                        f"runtime role {role_name!r} can execute agentcow.{function_name}"
                    )
                if role_kind == "reviewer":
                    should_execute = function_name in reviewer_function_names
                    if can_execute != should_execute:
                        expectation = "cannot" if should_execute else "can"
                        violations.append(
                            f"reviewer role {role_name!r} {expectation} execute "
                            f"agentcow.{function_name}"
                        )
                    if should_execute and (
                        not security_definer
                        or "search_path=pg_catalog" not in proconfig
                    ):
                        violations.append(
                            f"reviewer function agentcow.{function_name} is not a "
                            "locked-down SECURITY DEFINER function"
                        )

            for view_name, base_table, _pk_cols in enabled_tables:
                relation_rows = await executor.execute(
                    "SELECT table_.relname::text, table_.relowner::bigint, "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_table_privilege(reachable.oid, table_.oid, 'SELECT')), "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_table_privilege(reachable.oid, table_.oid, 'INSERT')), "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_table_privilege(reachable.oid, table_.oid, 'UPDATE')), "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_table_privilege(reachable.oid, table_.oid, 'DELETE')), "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_table_privilege(reachable.oid, table_.oid, 'TRUNCATE')), "
                    f"pg_has_role({role_oid}::oid, table_.relowner, 'MEMBER') "
                    "FROM pg_catalog.pg_class table_ "
                    "JOIN pg_catalog.pg_namespace namespace_ "
                    "ON namespace_.oid = table_.relnamespace "
                    f"WHERE namespace_.nspname = {_quote_literal(schema)} "
                    f"AND table_.relname IN ({_quote_literal(view_name)}, "
                    f"{_quote_literal(base_table)}, "
                    f"{_quote_literal(view_name + '_changes')}, "
                    f"{_quote_literal('cow_dirty_tables')})"
                )
                by_name = {row[0]: row[1:] for row in relation_rows}
                view_privileges = by_name.get(view_name)
                if view_privileges is None:
                    violations.append(f"COW view {schema}.{view_name} is missing")
                    continue
                _owner, select, insert, update, delete, truncate, owner_member = (
                    view_privileges
                )
                expected = (
                    (True, True, True, True)
                    if role_kind == "runtime"
                    else (True, False, False, False)
                )
                if (select, insert, update, delete) != expected:
                    violations.append(
                        f"{role_kind} role {role_name!r} has incorrect privileges "
                        f"on view {schema}.{view_name}: "
                        f"{(select, insert, update, delete)}"
                    )
                if truncate or owner_member:
                    violations.append(
                        f"{role_kind} role {role_name!r} controls view "
                        f"{schema}.{view_name}"
                    )

                for internal_name in (
                    base_table,
                    view_name + "_changes",
                    "cow_dirty_tables",
                ):
                    internal = by_name.get(internal_name)
                    if internal is None:
                        violations.append(
                            f"managed relation {schema}.{internal_name} is missing"
                        )
                        continue
                    _owner, select, insert, update, delete, truncate, owner_member = (
                        internal
                    )
                    if any((select, insert, update, delete, truncate, owner_member)):
                        violations.append(
                            f"{role_kind} role {role_name!r} has effective access "
                            f"to {schema}.{internal_name}"
                        )

                sequence_rows = await executor.execute(
                    "SELECT "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_sequence_privilege(reachable.oid, sequence_.oid, 'USAGE')), "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_sequence_privilege(reachable.oid, sequence_.oid, 'SELECT')), "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_sequence_privilege(reachable.oid, sequence_.oid, 'UPDATE')), "
                    f"pg_has_role({role_oid}::oid, sequence_.relowner, 'MEMBER') "
                    "FROM pg_catalog.pg_class sequence_ "
                    "JOIN pg_catalog.pg_namespace namespace_ "
                    "ON namespace_.oid = sequence_.relnamespace "
                    f"WHERE namespace_.nspname = {_quote_literal(schema)} "
                    "AND sequence_.relname = '_cow_operation_order_seq' "
                    "AND sequence_.relkind = 'S'"
                )
                if not sequence_rows:
                    violations.append(
                        f"managed sequence {schema}._cow_operation_order_seq is missing"
                    )
                elif any(sequence_rows[0]):
                    violations.append(
                        f"{role_kind} role {role_name!r} has effective sequence "
                        f"access in schema {schema!r}"
                    )

                generated_rows = await executor.execute(
                    "SELECT proc.proname::text, proc.prosecdef, proc.proowner::bigint, "
                    "COALESCE(array_to_string(proc.proconfig, ','), ''), "
                    "EXISTS (SELECT 1 FROM pg_catalog.pg_roles reachable "
                    f"WHERE pg_has_role({role_oid}::oid, reachable.oid, 'MEMBER') "
                    "AND has_function_privilege(reachable.oid, proc.oid, 'EXECUTE')), "
                    f"pg_has_role({role_oid}::oid, proc.proowner, 'MEMBER') "
                    "FROM pg_catalog.pg_proc proc "
                    "JOIN pg_catalog.pg_namespace namespace_ "
                    "ON namespace_.oid = proc.pronamespace "
                    f"WHERE namespace_.nspname = {_quote_literal(schema)} "
                    f"AND proc.proname IN ({_quote_literal(view_name + '_cow_upsert')}, "
                    f"{_quote_literal(view_name + '_cow_delete')})"
                )
                for (
                    fn_name,
                    secdef,
                    _owner,
                    config,
                    can_execute,
                    owner_member,
                ) in generated_rows:
                    if not secdef or "search_path=pg_catalog" not in config:
                        violations.append(
                            f"generated trigger {schema}.{fn_name} is not locked down"
                        )
                    if can_execute or owner_member:
                        violations.append(
                            f"{role_kind} role {role_name!r} controls generated "
                            f"trigger {schema}.{fn_name}"
                        )

    return CowPrivilegeValidation(safe=not violations, violations=violations)


# ---------------------------------------------------------------------------
# Enable / Disable COW
# ---------------------------------------------------------------------------


async def enable_cow(
    executor: Executor,
    table_name: str,
    pk_cols: list[str] | None = None,
    schema: str = "public",
    allow_deferred_fks: bool = False,
    allow_unsafe_canonical_writes: bool = False,
) -> None:
    """Enable COW on *table_name*.

    If *pk_cols* is ``None`` they are auto-detected from the database.

    Writes fail closed when transaction-local session or operation context is
    missing. Set *allow_unsafe_canonical_writes* to ``True`` only to opt into
    the upstream-compatible behavior that writes canonical state through the
    view without COW context.

    If *allow_deferred_fks* is ``True``, any ``NOT DEFERRABLE`` FK
    constraints on the base table are flipped to
    ``DEFERRABLE INITIALLY IMMEDIATE`` so that callers can use
    :func:`deferred_fk_constraints` or
    ``commit_cow_session_schema(..., defer_fk_constraints=True)``.
    This modifies your schema; by default we leave constraints alone
    and rely on topological-order commits to satisfy FK checks.
    """
    base_table = f"{table_name}_base"

    rows = await executor.execute(check_cow_state_sql(schema, table_name, base_table))
    base_exists, original_is_table, view_exists = rows[0]

    if pk_cols is None:
        pk_source = base_table if base_exists else table_name
        pk_cols = await _get_pk_cols(executor, schema, pk_source)

    if base_exists and view_exists:
        await executor.execute(
            setup_cow_sql(
                schema,
                base_table,
                table_name,
                pk_cols,
                fail_closed_writes=not allow_unsafe_canonical_writes,
            )
        )
        if allow_deferred_fks:
            await executor.execute(
                alter_fk_constraints_deferrable_sql(schema, base_table)
            )
        return

    if base_exists and not view_exists:
        await executor.execute(
            setup_cow_sql(
                schema,
                base_table,
                table_name,
                pk_cols,
                fail_closed_writes=not allow_unsafe_canonical_writes,
            )
        )
    elif original_is_table:
        await executor.execute(rename_table_sql(schema, table_name, base_table))
        await executor.execute(
            setup_cow_sql(
                schema,
                base_table,
                table_name,
                pk_cols,
                fail_closed_writes=not allow_unsafe_canonical_writes,
            )
        )
    else:
        raise ValueError(
            f"Table {table_name} not found in schema {schema} as table or view"
        )

    if allow_deferred_fks:
        await executor.execute(alter_fk_constraints_deferrable_sql(schema, base_table))


async def disable_cow(
    executor: Executor,
    table_name: str,
    schema: str = "public",
    revert_deferred_fks: bool = True,
) -> None:
    """Disable COW for *table_name*, restoring the original base table.

    If *revert_deferred_fks* is ``True`` (the default), any
    ``DEFERRABLE INITIALLY IMMEDIATE`` FK constraints on the base table
    are flipped back to ``NOT DEFERRABLE`` before the table is renamed.
    Constraints that are ``INITIALLY DEFERRED`` (explicitly set by the
    schema owner) are left alone.
    """
    base_table = f"{table_name}_base"
    changes_table = f"{table_name}_changes"

    rows = await executor.execute(
        check_cow_disable_state_sql(schema, table_name, base_table, changes_table)
    )
    base_exists, _original_is_table, view_exists, changes_exists = rows[0]

    if not base_exists and not view_exists and not changes_exists:
        return

    if base_exists and revert_deferred_fks:
        await executor.execute(
            alter_fk_constraints_not_deferrable_sql(schema, base_table)
        )

    await executor.execute(teardown_cow_sql(schema, table_name))

    if base_exists:
        check = await executor.execute(
            check_table_is_base_table_sql(schema, table_name)
        )
        if not check:
            await executor.execute(rename_table_sql(schema, base_table, table_name))


async def enable_cow_schema(
    executor: Executor,
    schema: str = "public",
    exclude: set[str] | None = None,
    allow_deferred_fks: bool = False,
    allow_unsafe_canonical_writes: bool = False,
) -> list[str]:
    """Enable COW on all user tables in *schema*.

    Tables whose names end with ``_base`` or ``_changes`` are skipped
    automatically, as are any names listed in *exclude*.

    See :func:`enable_cow` for *allow_deferred_fks*.

    Returns the table names that were enabled.
    """
    exclude = exclude or set()
    rows = await executor.execute(list_user_tables_sql(schema))
    already_cow = {
        row[0].removesuffix("_base")
        for row in await executor.execute(list_base_tables_sql(schema))
    }

    for table_name in sorted(already_cow - exclude):
        await enable_cow(
            executor,
            table_name,
            schema=schema,
            allow_deferred_fks=allow_deferred_fks,
            allow_unsafe_canonical_writes=allow_unsafe_canonical_writes,
        )

    enabled: list[str] = []
    for (table_name,) in rows:
        if table_name in exclude or table_name in already_cow:
            continue
        await enable_cow(
            executor,
            table_name,
            schema=schema,
            allow_deferred_fks=allow_deferred_fks,
            allow_unsafe_canonical_writes=allow_unsafe_canonical_writes,
        )
        enabled.append(table_name)
    return enabled


async def disable_cow_schema(
    executor: Executor,
    schema: str = "public",
    exclude: set[str] | None = None,
    revert_deferred_fks: bool = True,
) -> list[str]:
    """Disable COW on all COW-enabled tables in *schema*.

    Returns the table names that were disabled.
    """
    exclude = exclude or set()
    rows = await executor.execute(list_base_tables_sql(schema))
    disabled: list[str] = []
    for (base_name,) in rows:
        table_name = base_name.removesuffix("_base")
        if table_name in exclude:
            continue
        await disable_cow(
            executor,
            table_name,
            schema=schema,
            revert_deferred_fks=revert_deferred_fks,
        )
        disabled.append(table_name)
    return disabled


# ---------------------------------------------------------------------------
# Session-level commit / discard
# ---------------------------------------------------------------------------


async def commit_cow_session(
    executor: Executor,
    table_name: str,
    session_id: str | uuid.UUID,
    pk_cols: list[str] | None = None,
    schema: str = "public",
) -> None:
    """Commit all COW changes for *session_id* on a single table."""
    base_table = f"{table_name}_base"
    if pk_cols is None:
        pk_cols = await _get_pk_cols(executor, schema, base_table)
    await executor.execute(
        commit_cow_session_sql(schema, base_table, pk_cols, session_id)
    )


async def discard_cow_session(
    executor: Executor,
    table_name: str,
    session_id: str | uuid.UUID,
    schema: str = "public",
) -> None:
    """Discard all COW changes for *session_id* on a single table."""
    base_table = f"{table_name}_base"
    await executor.execute(discard_cow_session_sql(schema, base_table, session_id))


async def get_dirty_tables(
    executor: Executor,
    session_id: str | uuid.UUID,
    schema: str = "public",
) -> list[str]:
    """Get the list of dirty table names for a session."""
    rows = await executor.execute(get_dirty_tables_sql(schema, session_id))
    return [row[0] for row in rows]


async def _get_fk_edges(
    executor: Executor,
    schema: str,
    base_tables: list[str],
) -> list[tuple[str, str]]:
    """Fetch FK edges ``(parent_view, child_view)`` among *base_tables*.

    Returns view-level names (``_base`` suffix stripped). Self-referential
    edges are dropped — they're a within-table ordering concern.
    """
    if not base_tables:
        return []
    rows = await executor.execute(get_cow_fk_edges_sql(schema, base_tables))
    edges: list[tuple[str, str]] = []
    for parent_base, child_base, is_self_ref in rows:
        if is_self_ref:
            continue
        edges.append(
            (
                parent_base.removesuffix("_base"),
                child_base.removesuffix("_base"),
            )
        )
    return edges


async def commit_cow_session_schema(
    executor: Executor,
    session_id: str | uuid.UUID,
    schema: str = "public",
    defer_fk_constraints: bool = False,
) -> list[str]:
    """Commit all dirty tables for a session in a schema.

    By default, tables are committed in two phases ordered by FK
    dependency: upserts are applied parents-first (topological order),
    then deletes are applied children-first (reverse topological order).
    This keeps the database consistent at every intermediate step without
    requiring any schema modifications.

    If the dirty-table subgraph contains an FK cycle, a :class:`ValueError`
    is raised; the caller can either break the cycle in their schema or
    opt in to deferred checks with ``defer_fk_constraints=True`` (which
    requires the FKs to be ``DEFERRABLE`` — see
    ``enable_cow(allow_deferred_fks=True)``).

    Returns the list of table names that were committed (in the order
    they were processed).
    """
    tables = await get_dirty_tables(executor, session_id, schema)
    if not tables:
        return []

    if defer_fk_constraints:
        async with deferred_fk_constraints(executor):
            for table_name in tables:
                await commit_cow_session(
                    executor, table_name, session_id, schema=schema
                )
        return tables

    base_tables = [f"{t}_base" for t in tables]
    edges = await _get_fk_edges(executor, schema, base_tables)
    ordered = _topologically_sort_tables(tables, edges)

    for table_name in ordered:
        base_table = f"{table_name}_base"
        pk_cols = await _get_pk_cols(executor, schema, base_table)
        await executor.execute(
            commit_cow_upsert_sql(schema, base_table, pk_cols, session_id)
        )

    for table_name in reversed(ordered):
        base_table = f"{table_name}_base"
        pk_cols = await _get_pk_cols(executor, schema, base_table)
        await executor.execute(
            commit_cow_delete_sql(schema, base_table, pk_cols, session_id)
        )

    for table_name in ordered:
        base_table = f"{table_name}_base"
        await executor.execute(commit_cow_cleanup_sql(schema, base_table, session_id))

    return ordered


async def discard_cow_session_schema(
    executor: Executor,
    session_id: str | uuid.UUID,
    schema: str = "public",
) -> list[str]:
    """Discard all dirty tables for a session in a schema.

    Returns the list of table names that were discarded.
    """
    tables = await get_dirty_tables(executor, session_id, schema)
    for table_name in tables:
        await discard_cow_session(executor, table_name, session_id, schema=schema)
    return tables


# ---------------------------------------------------------------------------
# Operation-level commit / discard
# ---------------------------------------------------------------------------


async def commit_cow_operations(
    executor: Executor,
    table_name: str,
    session_id: str | uuid.UUID,
    operation_ids: list[str | uuid.UUID],
    pk_cols: list[str] | None = None,
    schema: str = "public",
) -> None:
    """Commit specific operations on a single table."""
    if not operation_ids:
        return
    base_table = f"{table_name}_base"
    if pk_cols is None:
        pk_cols = await _get_pk_cols(executor, schema, base_table)
    await executor.execute(
        commit_cow_operations_sql(
            schema, base_table, pk_cols, session_id, operation_ids
        )
    )


async def discard_cow_operations(
    executor: Executor,
    table_name: str,
    session_id: str | uuid.UUID,
    operation_ids: list[str | uuid.UUID],
    schema: str = "public",
) -> None:
    """Discard specific operations on a single table."""
    if not operation_ids:
        return
    base_table = f"{table_name}_base"
    await executor.execute(
        discard_cow_operations_sql(schema, base_table, session_id, operation_ids)
    )


# ---------------------------------------------------------------------------
# Querying operations
# ---------------------------------------------------------------------------


async def get_session_operations(
    executor: Executor,
    session_id: str | uuid.UUID,
    schema: str = "public",
) -> list[uuid.UUID]:
    """Get all operation IDs in a COW session."""
    rows = await executor.execute(get_session_operations_sql(schema, session_id))
    return [row[0] for row in rows]


async def get_operation_dependencies(
    executor: Executor,
    session_id: str | uuid.UUID,
    schema: str = "public",
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Get dependency pairs (depends_on, operation_id) in a session."""
    rows = await executor.execute(get_operation_dependencies_sql(schema, session_id))
    return [(row[0], row[1]) for row in rows]


async def set_visible_operations(
    executor: Executor,
    operation_ids: list[str | uuid.UUID] | None,
) -> None:
    """Set which operations' changes should be visible in subsequent queries."""
    await executor.execute(set_visible_operations_sql(operation_ids))


# ---------------------------------------------------------------------------
# Session variables
# ---------------------------------------------------------------------------


async def apply_cow_variables(
    executor: Executor,
    session_id: str | uuid.UUID,
    operation_id: str | uuid.UUID | None = None,
    visible_operations: list[str | uuid.UUID] | None = None,
) -> None:
    """Set the COW session variables (session_id, operation_id, visible_operations)."""
    for stmt in build_cow_variable_statements(
        session_id, operation_id, visible_operations
    ):
        await executor.execute(stmt)


async def reset_cow_variables(executor: Executor) -> None:
    """Reset all COW session variables to their defaults."""
    await executor.execute("RESET app.session_id")
    await executor.execute("RESET app.operation_id")
    await executor.execute("RESET app.visible_operations")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def is_cow_enabled(
    executor: Executor,
    config: CowPostgresConfig,
    schema: str = "public",
) -> bool:
    """Check whether CoW is both requested and properly configured.

    Returns ``True`` only when the request carries a session ID *and* the
    database has the CoW functions deployed with at least one CoW-enabled table.
    """
    if not config.is_active:
        return False

    func_rows = await executor.execute(check_cow_functions_deployed_sql())
    expected = len(COW_FUNCTION_NAMES)
    if (func_rows[0][0] if func_rows else 0) != expected:
        return False

    base_rows = await executor.execute(list_base_tables_sql(schema))
    return len(base_rows) > 0


async def get_cow_status(
    executor: Executor,
    schema: str = "public",
) -> CowStatus:
    """Get the COW status for a schema."""
    func_rows = await executor.execute(check_cow_functions_deployed_sql())
    expected = len(COW_FUNCTION_NAMES)
    cow_functions_deployed = (func_rows[0][0] if func_rows else 0) == expected

    base_rows = await executor.execute(list_base_tables_sql(schema))
    base_tables = [row[0] for row in base_rows]

    changes_rows = await executor.execute(list_changes_tables_sql(schema))
    changes_tables = [row[0] for row in changes_rows]

    tables_with_cow = [t.replace("_base", "") for t in base_tables]

    return CowStatus(
        enabled=len(base_tables) > 0,
        tables_with_cow=tables_with_cow,
        changes_tables=changes_tables,
        cow_functions_deployed=cow_functions_deployed,
    )
