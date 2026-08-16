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
COW changes table in the schema is torn down. Because trigger functions remain
security-invoker functions, a non-owner role that performs COW writes needs
`USAGE` on the schema's `_cow_operation_order_seq` in addition to its existing
table privileges.

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

H03 must still define and test ownership, `USAGE`, `EXECUTE`, and table/sequence
grants for the control and application schemas. H02 grants `PUBLIC` only
`USAGE` on the new control schema to preserve the accessibility of helpers
historically deployed in `public`; existing default function `EXECUTE`
behavior is otherwise unchanged. H02 intentionally does not redesign
PostgreSQL roles or change the current security-invoker model.

This ordering may change after review, but work should remain PR-sized. Tests
for a hardening item should be introduced with its corresponding fix rather
than publishing private audit artifacts independently.
