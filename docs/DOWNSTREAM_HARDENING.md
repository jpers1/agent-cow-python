# Downstream hardening scope

## Purpose

This maintained fork extends the upstream `agent-cow` library with bounded
PostgreSQL correctness, isolation, and integration improvements. It remains a
generic copy-on-write engine and is not the SLAIF Agent-State product.

The work described here is provisional. Each change requires its own narrow
work order, regression evidence, compatibility review, and human-approved
merge. This document does not claim that any item has already been
implemented.

## PostgreSQL subsystem

The downstream fork intends to improve:

- deterministic operation ordering;
- schema-safe internal SQL and object references;
- privilege-boundary ergonomics;
- fail-closed session operation modes;
- safer transaction and promotion APIs;
- conflict-detection primitives;
- safe integration examples;
- documentation of required PostgreSQL role separation.

Hardening should use PostgreSQL-native semantics, make unsafe preconditions
explicit, and preserve upstream behavior where doing so does not compromise a
documented correctness boundary.

## Blob subsystem

The existing blob subsystem remains upstream-derived code but is not currently
part of the SLAIF Agent-State integration target.

It remains in the repository for upstream compatibility and possible generic
use. Its inclusion does not make it an approved SLAIF media architecture, and
this governance change neither modifies nor removes it.

## Compatibility

The goal is to preserve upstream public APIs where reasonably possible.
Additive configuration, explicit strict modes, and narrowly scoped primitives
are preferred when they can provide the required guarantees. Any breaking API
or behavior change requires explicit human approval and clear migration
documentation.

## Provisional implementation sequence

```text
H01 — deterministic operation ordering
H02 — schema-qualified internal registry/functions
H03 — privilege and role hardening
H04 — strict/fail-closed session mode
H05 — safe server-owned session integration examples
H06 — conflict-detection support
H07 — transaction-safe promotion API
H08 — PostgreSQL compatibility/version matrix
H09 — downstream dependency/test cleanup
```

### H01 implementation status and design

H01 uses one schema-qualified PostgreSQL sequence named
`_cow_operation_order_seq` per COW-enabled schema. Every change row stores its
assigned value in `_cow_order`; all COW tables in the schema therefore share a
single monotonic order domain. Trigger conflict-update paths also consume and
store a new value. Rollback gaps are expected and do not affect correctness.

`_cow_updated_at` remains human-readable timestamp metadata. Overlay, commit,
dependency, and scoring paths use `_cow_order` whenever they need causal
ordering.

The sequence is owned by the schema owner and is dropped when the last ordered
COW changes table in the schema is torn down. H03 removes runtime sequence
access in a hardened schema by running the generated write triggers with the
controlled setup owner's privileges.

Deploying the downstream functions automatically upgrades an enabled
upstream-format COW table only when its legacy changes table is empty. If
pending legacy rows exist, deployment fails before replacing functions because
their historical order cannot be reconstructed safely. Applications must
commit or discard those rows with the previous version before retrying the
upgrade.

### H02 implementation status and design

H02 places deployed control functions in the dedicated `agentcow` schema and
qualifies every call to those functions. Each COW-enabled application schema
owns its own `cow_dirty_tables` registry, `_cow_operation_order_seq`, changes
tables, overlay views, and generated trigger functions. Generated SQL uses
quoted, schema-qualified identifiers, and deployed functions run with
`search_path = pg_catalog`.

Agent-cow internal PostgreSQL objects are therefore explicitly
schema-resolved and do not depend on application-controlled `search_path`.
Temporary or attacker-schema objects with the same names cannot redirect dirty
tracking, commit, discard, dependency discovery, or teardown. Existing
`public` installations retain their registry in `public`; H01-era registry
entries for a non-public application schema are moved transactionally into
that schema when the enabled table is redeployed.

H03 removes H02's temporary `PUBLIC` compatibility grants and defines the
ownership, `USAGE`, `EXECUTE`, table, and sequence boundaries described below.

### H03 implementation status and design

H03 adds `harden_cow_schema(...)` for caller-supplied setup, runtime, and
reviewer roles. Hardened runtime roles receive CRUD only on COW views. The
generated write triggers become setup-owned `SECURITY DEFINER` functions with
locked `pg_catalog` search paths and fully qualified application objects, so
runtime roles need no direct change-table, registry, or sequence privilege.

Reviewers receive view `SELECT` plus a narrow set of controlled inspection,
dependency, commit, and discard functions. Setup and teardown remain
owner-only invoker operations. The control schema and all deployed functions
revoke default `PUBLIC` authority.

Writes now fail closed by default when either transaction-local session or
operation context is missing or malformed. The historical canonical
write-through behavior requires the explicit
`allow_unsafe_canonical_writes=True` compatibility option and is not permitted
by the hardened role model.

`validate_cow_schema_privileges(...)` checks effective privileges through
direct grants, `PUBLIC`, ownership, inheritance, and every role reachable with
`SET ROLE`. Unsafe inherited access is reported rather than revoked from an
unlisted role. Apply hardening in an explicit transaction and roll it back if
validation fails. The full deployment works with a non-superuser setup owner.

Session UUID selection remains a trusted-application responsibility: a shared
runtime role can set custom PostgreSQL GUC values. Database credentials and
session selection must remain inside the trusted gateway. See
[`POSTGRES_SECURITY_MODEL.md`](POSTGRES_SECURITY_MODEL.md).

This ordering may change after review, but work should remain PR-sized. Tests
for a hardening item should be introduced with its corresponding fix rather
than publishing private audit artifacts independently.
