"""Base COW configuration for the PostgreSQL implementation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class CowConfig:
    """Base configuration extended by PostgreSQL-specific settings."""

    session_id: uuid.UUID | None = None
    operation_id: uuid.UUID | None = None

    @property
    def is_active(self) -> bool:
        return self.session_id is not None
