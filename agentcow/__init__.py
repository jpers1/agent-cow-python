"""agent-cow: Database Copy-On-Write for AI agent workspace isolation."""

__version__ = "0.2.0rc1"

from .context import CowConfig

__all__ = ["CowConfig"]

# blob subpackage available via `from agentcow.blob import ...`
