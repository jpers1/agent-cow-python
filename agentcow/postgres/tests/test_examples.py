"""Keep the public hardened-integration examples importable and on-policy."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import agentcow.postgres as postgres

POSTGRES_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = POSTGRES_ROOT / "examples"
RECOMMENDED = EXAMPLES / "asyncpg_safe_session_example.py"
SQLALCHEMY = EXAMPLES / "sqlalchemy_example.py"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_examples_compile_and_import() -> None:
    for path in sorted(EXAMPLES.glob("*.py")):
        compile(path.read_text(), str(path), "exec")
        _load(path)


def test_recommended_asyncpg_example_uses_hardened_public_apis() -> None:
    source = RECOMMENDED.read_text()
    assert "asyncpg.create_pool" in source
    assert "asyncpg_cow_session" in source
    assert "harden_cow_schema" in source
    assert "validate_cow_schema_privileges" in source
    assert "asyncpg_cow_reviewer" in source
    assert "reviewer.commit_session" in source
    assert "reviewer.discard_session" in source
    assert "reviewer_pool.acquire" not in source
    assert "SET LOCAL" not in source
    assert "x-cow-session-id" not in source.lower()
    assert "allow_unsafe_canonical_writes" not in source


def test_sqlalchemy_example_is_optional_and_uses_safe_scope() -> None:
    source = SQLALCHEMY.read_text()
    tree = ast.parse(source)
    top_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("sqlalchemy")
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("sqlalchemy") for alias in node.names)
        )
        for node in top_level_imports
    )
    assert "sqlalchemy_cow_session" in source
    assert "SET LOCAL" not in source


def test_documented_hardened_public_api_names_exist() -> None:
    names = {
        "asyncpg_cow_session",
        "sqlalchemy_cow_session",
        "asyncpg_cow_reviewer",
        "sqlalchemy_cow_reviewer",
        "CowReviewer",
        "CowConflictError",
        "PromotionResult",
        "DiscardResult",
        "harden_cow_schema",
        "validate_cow_schema_privileges",
        "get_session_operations",
        "get_operation_dependencies",
        "commit_cow_session_schema",
        "discard_cow_session_schema",
    }
    assert all(hasattr(postgres, name) for name in names)
