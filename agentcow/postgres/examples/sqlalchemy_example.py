"""Optional SQLAlchemy asyncio integration using the safe H04 session scope.

Install ``agent-cow[sqlalchemy]`` to run this example.  SQLAlchemy imports are
kept inside the example functions so importing this module does not make the
optional adapter a core dependency.

The caller must supply a trusted, server-selected session UUID.  Transport
authentication and capability lookup happen before this library boundary.
"""

from __future__ import annotations

import uuid
from typing import Any

from agentcow.postgres import sqlalchemy_cow_session


class ExampleRequestError(RuntimeError):
    """Application-level failure used only to demonstrate rollback."""


async def create_session_factory(database_url: str) -> tuple[Any, Any]:
    """Create an optional SQLAlchemy async engine and session factory."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def run_authorized_request(
    session_factory: Any,
    *,
    trusted_session_id: uuid.UUID,
    page_id: int,
    title: str,
) -> None:
    """Run parameterized ORM-layer work in one owned COW transaction.

    ``cow.native`` is used only by trusted application repository code.  It
    must never be exposed as arbitrary SQL access to an external agent.
    """
    from sqlalchemy import text

    async with sqlalchemy_cow_session(
        session_factory,
        session_id=trusted_session_id,
    ) as cow:
        await cow.validate_context()
        await cow.native.execute(
            text("INSERT INTO content.pages (id, title) " "VALUES (:page_id, :title)"),
            {"page_id": page_id, "title": title},
        )

        await cow.set_operation()
        await cow.validate_context()
        await cow.native.execute(
            text("UPDATE content.pages SET title = :title " "WHERE id = :page_id"),
            {"page_id": page_id, "title": f"{title} (revised)"},
        )


async def error_paths_roll_back(
    session_factory: Any, trusted_session_id: uuid.UUID
) -> None:
    """Illustrate that exceptions leave no request transaction committed."""
    try:
        async with sqlalchemy_cow_session(
            session_factory, session_id=trusted_session_id
        ) as cow:
            await cow.execute(
                "INSERT INTO content.pages (id, title) VALUES (1003, 'temporary')"
            )
            raise ExampleRequestError("application request failed")
    except ExampleRequestError:
        pass
