# AGENTS.md

## Repository identity

`agent-cow-postgresql` is an MIT-licensed PostgreSQL-focused downstream fork of
[`trail-ml/agent-cow-python`](https://github.com/trail-ml/agent-cow-python),
maintained by `jpers1`. Preserve upstream attribution, copyright notices,
license terms, authorship, and Git history.

The fork hardens and extends the PostgreSQL copy-on-write engine for generic
AI-agent and application workflows. The maintained and shipped scope is
PostgreSQL only. It is a reusable library, not an application product. Do not
add product services, gateways, UIs, or application-specific policy here.

## Remote boundary

The expected remotes are:

```text
origin   https://github.com/jpers1/agent-cow-postgresql.git
upstream https://github.com/trail-ml/agent-cow-python.git
```

- Push only to `origin` unless the human lead explicitly authorizes otherwise.
- Never open an upstream issue or pull request without explicit human
  authorization.
- Do not rewrite, squash, or obscure upstream history.
- Preserve the upstream MIT license and attribution.
- Keep private audit evidence and finding records out of this public fork.

## Authority

The human lead owns:

- merge and release decisions;
- upstream disclosure and contribution decisions;
- architecture changes;
- final compatibility and dependency policy.

The coding agent may, when authorized by a work order:

- create a narrow branch;
- implement the requested change;
- run tests;
- commit and push to `origin`;
- open a draft pull request.

The coding agent must not merge its own pull request or expand a work order
into unrelated changes.

## Hardening principles

- Prefer small, reviewable changes.
- Add focused regression tests before or with fixes.
- Preserve upstream public APIs where practical.
- Prefer explicit fail-closed behavior at security and correctness boundaries.
- Use PostgreSQL-native transactional and schema semantics.
- Minimize new dependencies and ownership burden.
- Preserve generic usefulness across downstream applications.
- Do not turn this library into a product-specific application.

Implementation fixes should state their compatibility impact, required
deployment assumptions, and behavior under failure. Breaking changes require
explicit human approval.

## Dependency policy

New dependencies require a demonstrated need. The default allowed licenses
are:

```text
MIT
Apache-2.0
BSD-2-Clause
BSD-3-Clause
PostgreSQL License
similarly permissive licenses after explicit review
```

Do not introduce new GPL, LGPL, AGPL, MPL, SSPL, BUSL, source-available,
commercial, or account-bound dependencies without explicit human
authorization. Do not remove historical upstream dependencies without a
separate approved work order.

For future tests, prefer:

- `pytest`;
- `asyncpg`;
- local PostgreSQL via OCI/Docker;
- Python standard-library tooling.

Builds and routine tests must not require production data, cloud credentials,
paid services, or account-bound hosted infrastructure.

## Working rules

Before changing the repository:

1. Verify the current branch, worktree, and remotes.
2. Start from current `origin/main` unless the work order says otherwise.
3. Inspect before editing and preserve unrelated worktree changes.
4. Change only files within the authorized scope.
5. Run focused tests and broader tests in proportion to the change.
6. Inspect the complete diff and run `git diff --check` before committing.
7. Stage explicit paths only; do not use `git add .`, `git add -A`, or
   `git add --all`.
8. Push only the requested branch to `origin` and open at most one draft PR.
9. Do not merge.
