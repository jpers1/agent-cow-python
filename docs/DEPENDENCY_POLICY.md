# Downstream dependency policy

## Goals

Runtime dependencies should remain minimal. Every new mandatory runtime
dependency requires human review of its necessity, maintenance impact,
license, provenance, and compatibility surface.

The standard build and test path does not require a commercial or account-bound
service. It runs locally with Git, Python, PostgreSQL, and OCI/Docker tooling,
without production data, real cloud credentials, or paid infrastructure.

## License policy

Permissive licenses are the default requirement for new runtime, development,
test, container, and tooling dependencies. Normally acceptable licenses are:

```text
MIT
Apache-2.0
BSD-2-Clause
BSD-3-Clause
PostgreSQL License
similarly permissive licenses after explicit review
```

Do not add a new GPL, LGPL, AGPL, MPL, SSPL, BUSL, source-available,
commercial, account-bound, or unclear dependency without explicit human
authorization. License review is an engineering policy check, not legal
advice.

## Development and testing

Downstream PostgreSQL tests use permissively licensed tools and small,
replaceable integration surfaces, including:

- `pytest`;
- `asyncpg`;
- direct local PostgreSQL orchestration through OCI/Docker;
- Python standard-library tooling.

Tests should be reproducible against disposable local services. Optional
adapter coverage may use isolated environments, but it must not silently
become a mandatory shipping dependency.

## Supported environment

The canonical development installation is:

```bash
uv sync --frozen --group dev
```

It includes the PostgreSQL implementation, the complete PostgreSQL test tree,
the asyncpg and optional SQLAlchemy adapters, Ruff, and the build frontend and
backend. It does not include blob-test dependencies. Validate the installed
environment with:

```bash
uv run python scripts/check_dependency_policy.py
```

The policy check uses installed distribution metadata plus a small reviewed
license/reason mapping and rejects unexpected packages. The full direct and
transitive inventory is maintained in
[`DEPENDENCY_INVENTORY.md`](DEPENDENCY_INVENTORY.md).

## Formatting and building

Ruff is the supported formatter/checker. Format only the Python files being
changed rather than applying an unrelated repository-wide rewrite:

```bash
uv run ruff format path/to/changed.py
uv run ruff check path/to/changed.py
```

The package build uses the installed, policy-checked Setuptools backend:

```bash
uv run python -m build --no-isolation --wheel --sdist
```

## Blob test scope

The blob implementation and its `boto3` runtime extra remain inherited
upstream functionality. Blob testing is outside the supported downstream
PostgreSQL development environment and does not pull `moto` or its dependency
graph into the standard `dev` group. This is a scope boundary, not a statement
that those tools are unlawful or defective.
