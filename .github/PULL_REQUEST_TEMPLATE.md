<!--
One logical change per PR. If this is a refactor AND a fix, split it.
-->

## What this changes

<!-- One paragraph: what behaves differently after this PR. -->

## Why it matters

Closes #

<!--
Put a real closing keyword above (Closes / Fixes / Resolves) with the issue
number. "Per #12" or "see #12" links nothing, does not close the issue, and
leaves it looking unclaimed so someone duplicates the work.

If this PR genuinely does not close an issue, delete the line and say why here.
-->

## How it was checked

- [ ] `make check` passes locally (ruff check + ruff format --check + pytest against a real Postgres)
- [ ] There is a test that **fails without this change**
- [ ] If the wire contract changed, `models.py` and `docs/protocol.md` moved together
- [ ] If a mission state transition changed, the constant in `core/sql.py` changed and a concurrency test covers it

## Scope

- [ ] One logical change
- [ ] No new dependency in the base install (`httpx` + `pydantic` only)
