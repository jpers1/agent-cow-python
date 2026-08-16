# Downstream dependency policy

## Goals

Runtime dependencies should remain minimal. Every new mandatory runtime
dependency requires human review of its necessity, maintenance impact,
license, provenance, and compatibility surface.

The standard build and test path must not require a commercial or
account-bound service. It should ultimately run locally with Git, Python,
PostgreSQL, and OCI/Docker tooling, without production data, real cloud
credentials, or paid infrastructure.

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

Future downstream tests should prefer permissively licensed tools and small,
replaceable integration surfaces, including:

- `pytest`;
- `asyncpg`;
- direct local PostgreSQL orchestration through OCI/Docker;
- Python standard-library tooling.

Tests should be reproducible against disposable local services. Optional
adapter coverage may use isolated environments, but it must not silently
become a mandatory shipping dependency.

## Existing upstream declarations

This policy governs downstream changes. Historical upstream dependency and
lock declarations remain unchanged until a separate work order evaluates and
authorizes cleanup. This governance PR does not modify `pyproject.toml` or
`uv.lock`.
