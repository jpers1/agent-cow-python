"""
Compatibility-only HTTP header parsing for agent-cow.

Utilities for extracting COW session configuration from HTTP request headers.
This is framework-agnostic — it works with any object that has a ``.headers``
dict (FastAPI, Starlette, Django, Flask, etc.).

Do not use these transport values directly as PostgreSQL COW identity in a
production integration. Authenticate the request and resolve it to trusted,
server-owned session and operation UUIDs first. The safe database lifecycle is
provided by ``asyncpg_cow_session`` or ``sqlalchemy_cow_session``.

The expected headers are:

    x-cow-session-id: UUID
    x-cow-operation-id: UUID
    x-cow-visible-operations: comma-separated UUIDs

Sections:
    1. Header parsing              -- extract COW config from a request
    2. FastAPI boundary example     -- trusted lookup before database context

Requirements:
    uv add agent-cow
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Header parsing
# =============================================================================


@dataclass
class CowHeaderConfig:
    """COW configuration parsed from HTTP request headers."""

    session_id: uuid.UUID | None = None
    operation_id: uuid.UUID | None = None
    visible_operations: list[uuid.UUID] | None = None

    @property
    def is_active(self) -> bool:
        return self.session_id is not None


def _parse_uuid_header(headers: Any, name: str) -> uuid.UUID | None:
    value = headers.get(name)
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        logger.warning("Invalid %s header: %r", name, value)
        return None


def _parse_uuid_list_header(headers: Any, name: str) -> list[uuid.UUID] | None:
    value = headers.get(name)
    if not value:
        return None
    try:
        return [uuid.UUID(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError:
        logger.warning("Invalid %s header: %r", name, value)
        return None


def parse_cow_headers(request) -> CowHeaderConfig:
    """Parse compatibility COW headers without authorizing their contents.

    Works with any request object that has a ``.headers`` mapping
    (FastAPI/Starlette ``Request``, Django ``HttpRequest``, Flask ``request``, etc.).

    Invalid header values are logged and treated as absent.
    """
    if request is None:
        return CowHeaderConfig()

    headers = request.headers
    return CowHeaderConfig(
        session_id=_parse_uuid_header(headers, "x-cow-session-id"),
        operation_id=_parse_uuid_header(headers, "x-cow-operation-id"),
        visible_operations=_parse_uuid_list_header(headers, "x-cow-visible-operations"),
    )


# =============================================================================
# 2. FastAPI middleware example
# =============================================================================
# The resolver below is application-owned authentication/authorization. It
# must return server-owned UUIDs, not pass raw headers through as DB identity.
#
#     from fastapi import FastAPI, Request
#     from agentcow.postgres import asyncpg_cow_session
#
#     app = FastAPI()
#
#     @app.middleware("http")
#     async def cow_middleware(request: Request, call_next):
#         authorized = await resolve_server_owned_cow_context(request)
#         async with asyncpg_cow_session(
#             request.app.state.database_pool,
#             session_id=authorized.session_id,
#             operation_id=authorized.operation_id,
#             visible_operations=authorized.visible_operations,
#         ) as cow:
#             request.state.database = cow
#             return await call_next(request)
