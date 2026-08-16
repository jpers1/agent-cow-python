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

This ordering may change after review, but work should remain PR-sized. Tests
for a hardening item should be introduced with its corresponding fix rather
than publishing private audit artifacts independently.
