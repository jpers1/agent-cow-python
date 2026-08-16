# Changelog

This changelog records maintained downstream changes. The fork preserves the
history and MIT attribution of `trail-ml/agent-cow-python`.

## 0.2.0 — 2026-08-16

First maintained PostgreSQL-focused downstream release. Derived from Trail's
MIT-licensed `agent-cow-python` `0.1.7` at commit
`d49d74e3f357d67bf5eda5377bbca50cdf3d952e`.

### Scope

- The maintained downstream distribution is now `agent-cow-postgresql`.
- The Python import namespace remains `agentcow`.
- The repository focuses exclusively on PostgreSQL.
- This PostgreSQL-focused fork intentionally does not ship the upstream
  blob-storage subsystem.

### Added

- Transaction-owning asyncpg and optional SQLAlchemy session scopes.
- Declarative PostgreSQL setup/runtime/reviewer role hardening and effective
  privilege validation.
- Row-level first-touch conflict inspection and conflict-safe promotion.
- Transaction-owning, multi-table reviewer promotion and discard APIs.
- Reproducible Python 3.10–3.14 and PostgreSQL 14–18 test matrix.
- Permissive-only supported PostgreSQL development and build environment.

### Changed

- Causal operation ordering now uses a schema-wide monotonic PostgreSQL
  sequence rather than timestamps.
- Internal control functions live in the explicitly resolved `agentcow`
  schema; generated functions use qualified objects and locked search paths.
- Hardened writes fail closed when session or operation context is absent or
  malformed. Canonical write-through is an explicit compatibility option.
- Promotion defaults to conflict rejection; historical last-writer-wins
  behavior requires the explicit `conflict_policy="overwrite"` option.
- Recommended integration uses server-selected UUIDs, one explicit
  transaction, and separate runtime and reviewer database roles.

### Security / isolation hardening

- Runtime roles no longer need direct base, changes, registry, or sequence
  access for CoW CRUD.
- Controlled `SECURITY DEFINER` trigger and reviewer functions use locked
  search paths and explicit object qualification.
- Whole-session and selective promotion/discard are atomic across affected
  tables through the high-level reviewer APIs.

### Compatibility

- The distribution is `agent-cow-postgresql`; the `agentcow` import namespace
  and upstream low-level Python APIs remain available.
- Supported Python versions are 3.10–3.14.
- Supported PostgreSQL versions are 14–18.
- Release artifacts are distributed through PyPI and GitHub Releases using a
  manual, tag-specific Trusted Publishing workflow.

### Migration notes

- Databases with no CoW-enabled tables can deploy and enable normally.
- An upstream `0.1.7` changes table is upgraded automatically only when it has
  no pending rows.
- Empty H01/H05-era changes tables receive missing order or conflict-baseline
  metadata automatically.
- Deployment refuses pending rows whose historical order or first-touch
  baseline cannot be reconstructed. Commit or discard them with the prior
  version before upgrading.
- After deploying new controlled functions, rerun `harden_cow_schema(...)` in
  the administrative transaction before runtime traffic resumes.
