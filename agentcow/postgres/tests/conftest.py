from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

import asyncpg
import pytest

PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "postgres")
PG_DBNAME = os.environ.get("PG_DBNAME", "agent_cow_test")


def quote_identifier(value: str) -> str:
    """Quote a PostgreSQL identifier used by the test harness."""
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """Quote a PostgreSQL string literal used by the test harness."""
    return "'" + value.replace("'", "''") + "'"


def has_multiple_sql_statements(value: str) -> bool:
    """Return whether SQL contains more than one top-level statement."""
    statements = 0
    content = False
    index = 0
    quote: str | None = None
    dollar_quote: str | None = None
    block_comment_depth = 0
    while index < len(value):
        current = value[index]
        following = value[index + 1] if index + 1 < len(value) else ""
        if block_comment_depth:
            if current == "/" and following == "*":
                block_comment_depth += 1
                index += 2
            elif current == "*" and following == "/":
                block_comment_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if dollar_quote:
            if value.startswith(dollar_quote, index):
                index += len(dollar_quote)
                dollar_quote = None
            else:
                index += 1
            continue
        if quote:
            if current == quote and following == quote:
                index += 2
            elif current == quote:
                quote = None
                index += 1
            else:
                index += 1
            continue
        if current == "-" and following == "-":
            newline = value.find("\n", index + 2)
            index = len(value) if newline == -1 else newline + 1
            continue
        if current == "/" and following == "*":
            block_comment_depth = 1
            index += 2
            continue
        if current in ("'", '"'):
            quote = current
            content = True
            index += 1
            continue
        if current == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$", value[index:])
            if match:
                dollar_quote = match.group(0)
                content = True
                index += len(dollar_quote)
                continue
        if current == ";":
            if content:
                statements += 1
                content = False
            index += 1
            continue
        if not current.isspace():
            content = True
        index += 1
    if content:
        statements += 1
    return statements > 1


@dataclass(frozen=True)
class ConnectionInfo:
    host: str
    port: int
    user: str
    dbname: str


class QueryResult:
    """Small DB-API-shaped result used by inherited synchronous assertions."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self._offset = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._offset == len(self._rows):
            return None
        row = self._rows[self._offset]
        self._offset += 1
        return row

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._rows[self._offset :]
        self._offset = len(self._rows)
        return rows


class _Transaction(AbstractContextManager[None]):
    def __init__(self, connection: AsyncpgTestConnection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.begin()
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        return False


class AsyncpgTestConnection(AbstractContextManager["AsyncpgTestConnection"]):
    """Synchronous test facade backed exclusively by an asyncpg connection.

    The original tests used Psycopg's implicit transaction behavior directly.
    This facade preserves that narrow test contract while running asyncpg on a
    dedicated event-loop thread. Production adapter coverage continues to use
    native ``asyncpg.Connection`` and ``asyncpg.Pool`` objects directly.
    """

    def __init__(
        self,
        *,
        host: str = PG_HOST,
        port: int = PG_PORT,
        user: str = PG_USER,
        password: str = PG_PASSWORD,
        database: str = PG_DBNAME,
        autocommit: bool = False,
        application_name: str | None = None,
    ) -> None:
        self.info = ConnectionInfo(host=host, port=port, user=user, dbname=database)
        self._autocommit = autocommit
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._connection: asyncpg.Connection | None = None
        self._transaction: asyncpg.Transaction | None = None
        server_settings = (
            {"application_name": application_name} if application_name else None
        )
        self._connection = self._submit(
            asyncpg.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                server_settings=server_settings,
            )
        )
        self._submit(self._configure_codecs())

    def _submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    async def _configure_codecs(self) -> None:
        assert self._connection is not None
        for type_name in ("json", "jsonb"):
            await self._connection.set_type_codec(
                type_name,
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
            )

    async def _start_transaction(self) -> None:
        assert self._connection is not None
        if self._transaction is not None:
            raise RuntimeError("test connection already has an active transaction")
        self._transaction = self._connection.transaction()
        await self._transaction.start()

    def begin(self) -> None:
        if self._autocommit:
            raise RuntimeError("autocommit test connection cannot begin a transaction")
        self._submit(self._start_transaction())

    async def _execute(self, statement: str, *arguments: Any) -> QueryResult:
        assert self._connection is not None
        if not self._autocommit and self._transaction is None:
            await self._start_transaction()
        if has_multiple_sql_statements(statement):
            if arguments:
                raise ValueError("multiple test SQL statements cannot use parameters")
            await self._connection.execute(statement)
            rows = []
        else:
            rows = await self._connection.fetch(statement, *arguments)
        return QueryResult([tuple(row) for row in rows])

    def execute(self, statement: str, *arguments: Any) -> QueryResult:
        return self._submit(self._execute(statement, *arguments))

    def cursor(self) -> AsyncpgTestConnection:
        return self

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def _finish_transaction(self, *, commit: bool) -> None:
        if self._transaction is None:
            return
        transaction = self._transaction
        self._transaction = None
        if commit:
            await transaction.commit()
        else:
            await transaction.rollback()

    def commit(self) -> None:
        self._submit(self._finish_transaction(commit=True))

    def rollback(self) -> None:
        self._submit(self._finish_transaction(commit=False))

    async def _close(self) -> None:
        if self._transaction is not None:
            await self._finish_transaction(commit=False)
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def close(self) -> None:
        if not self._loop.is_running():
            return
        self._submit(self._close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    def __enter__(self) -> AsyncpgTestConnection:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


def connect_test_database(
    database: str,
    *,
    role: str = PG_USER,
    autocommit: bool = False,
    application_name: str | None = None,
) -> AsyncpgTestConnection:
    return AsyncpgTestConnection(
        user=role,
        database=database,
        autocommit=autocommit,
        application_name=application_name,
    )


def _recreate_test_db() -> None:
    with connect_test_database("postgres", autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {quote_literal(PG_DBNAME)} "
            "AND pid <> pg_backend_pid()"
        )
        connection.execute(f"DROP DATABASE IF EXISTS {quote_identifier(PG_DBNAME)}")
        connection.execute(f"CREATE DATABASE {quote_identifier(PG_DBNAME)}")


def _drop_test_db() -> None:
    with connect_test_database("postgres", autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {quote_literal(PG_DBNAME)} "
            "AND pid <> pg_backend_pid()"
        )
        connection.execute(f"DROP DATABASE IF EXISTS {quote_identifier(PG_DBNAME)}")


@pytest.fixture(scope="session", autouse=True)
def _test_database_lifecycle():
    _drop_test_db()
    yield
    _drop_test_db()


@pytest.fixture
def postgresql():
    _recreate_test_db()
    connection = connect_test_database(PG_DBNAME)
    try:
        yield connection
    finally:
        connection.close()


class AsyncpgExecutor:
    """Wrap a native or test-facade asyncpg connection as an Executor."""

    def __init__(self, connection: AsyncpgTestConnection | asyncpg.Connection):
        self._conn = connection

    async def execute(self, statement: str) -> list[tuple[Any, ...]]:
        if isinstance(self._conn, AsyncpgTestConnection):
            return self._conn.execute(statement).fetchall()
        return [tuple(row) for row in await self._conn.fetch(statement)]

    def commit(self) -> None:
        if not isinstance(self._conn, AsyncpgTestConnection):
            raise RuntimeError("native asyncpg transaction ownership is caller-managed")
        self._conn.commit()


SEED_SQL = """
CREATE TABLE users (
    id serial PRIMARY KEY,
    name text NOT NULL,
    email text UNIQUE NOT NULL
);

CREATE TABLE projects (
    id serial PRIMARY KEY,
    owner_id integer NOT NULL REFERENCES users(id),
    title text NOT NULL,
    description text DEFAULT ''
);

CREATE TABLE tasks (
    id serial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects(id),
    assigned_to integer REFERENCES users(id),
    title text NOT NULL,
    done boolean NOT NULL DEFAULT false
);

INSERT INTO users (name, email) VALUES
    ('Bessie', 'bessie@sunnymeadow.farm'),
    ('Clyde',  'clyde@lonepine.farm');

INSERT INTO projects (owner_id, title, description) VALUES
    (1, 'North Pasture', 'Grazing rotation and fence maintenance'),
    (2, 'Dairy Barn',    'Milk production and storage');

INSERT INTO tasks (project_id, assigned_to, title) VALUES
    (1, 1, 'Repair fencing'),
    (1, 2, 'Rotate hay bales'),
    (2, 1, 'Install milking equipment');
"""


@pytest.fixture
def executor(postgresql):
    return AsyncpgExecutor(postgresql)


@pytest.fixture
def seeded_executor(postgresql):
    """Executor with users, projects, and tasks tables already populated."""
    postgresql.execute(SEED_SQL)
    postgresql.commit()
    return AsyncpgExecutor(postgresql)


@pytest.fixture
def session_id():
    return uuid.uuid4()


@pytest.fixture
def operation_id():
    return uuid.uuid4()
