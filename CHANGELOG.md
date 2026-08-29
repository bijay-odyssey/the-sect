# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). This
is a `0.x` project: until `1.0.0`, minor versions may change the HTTP API, the wire
models, and the database schema. Migrations are additive and run on boot.

The `0.1.0` and `0.2.0` entries were reconstructed from git history — neither was tagged
at the time.

## [Unreleased]

## [0.2.1] - 2026-08-29

### Added

- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md`, and
  this changelog.
- Issue forms and a pull-request template under `.github/`.
- Regression coverage for the SDK's `Retry-After` handling — both the seconds and the
  HTTP-date form — and the backoff cap (#21, thanks @mikemikimike).

### Fixed

- README: install commands now show the git install (the package is not on PyPI yet);
  corrected the stale "two tables" count and an unverifiable "seven of them" claim;
  removed a stray horizontal rule and a broken relative link.
- `/health` logs the database probe failure at warning level instead of swallowing it
  (#20, thanks @mikemikimike).
- Unhandled exceptions return the `{"error": {…}}` JSON envelope instead of a bare
  plain-text 500 (#24, thanks @mikemikimike).
- Migration discovery rejects duplicate or missing version numbers at startup, naming
  the offending files, instead of applying them in filename order (#23, thanks
  @mikemikimike).
- The `peak-template` config reader keeps `#` inside single- or double-quoted values
  (#22, thanks @mikemikimike).

## [0.2.0] - 2026-08-28

### Added

- **Peaks.** A `peaks` table and master-only CRUD at `/v1/peaks`; `disciples.peak_id` and
  an optional `missions.peak_id`. Routing is **advisory**: `peak_id` only reorders
  `claim-next` and the open board so a disciple sees its own peak's work first — it never
  gates a claim.
- **Contribution ledger.** `contribution_points`, `completed_missions`, `failed_missions`,
  `success_rate`, and `reputation` on `disciples`, moved in the same statement as the
  mission outcome via a data-modifying CTE. `0002_peaks.sql` backfills them from history.
- `SECT_FAILURE_POINT_PENALTY` (default `0`) to optionally dock contribution points on a
  terminal failure.
- Structured JSON logging: `SECT_LOG_JSON` (default on) and a per-request access line
  carrying method, path, status, duration, disciple, and mission id.
- `sect disciple create --peak`, and a `peak=` argument on the SDK's `Disciple` and
  `SectMaster.register_disciple`.
- `peak-template/` — a fork-and-fill-in starting point for a new peak.
- `Makefile` with `install`, `pg`, `test`, `lint`, `fmt`, `check`, `migrate`, `status`,
  `run`, `tag`, `clean`.
- Test suites `test_peaks.py`, `test_contributions.py`, `test_auth.py`, and
  `test_realms_match_database.py`. The last two were referenced in the docs but had never
  existed; `test_realms_match_database.py` is the guard that the realm `Literal` and the
  database `CHECK` cannot drift apart.

### Changed

- Disciple objects now carry `peak` and the five ledger fields; mission objects carry
  `peak`. All additive — a Sect with no peaks behaves exactly as in `0.1.0`.

### Fixed

- The `YOUR-USERNAME` placeholders in `examples/disciple-scribe/` now point at the real
  repository.

## [0.1.0] - 2026-08-13

Initial version.

### Added

- **Atomic mission claiming.** Every mission state transition is a single guarded
  `UPDATE` under `READ COMMITTED`: `claim`, `claim-next` (`FOR UPDATE SKIP LOCKED`),
  `heartbeat`, `complete`, `fail`, `cancel`. The winner of a race gets a row back;
  everyone else matches zero rows and gets a `409`.
- **Leases.** Every claim carries a server-issued expiry and a per-claim token, so a
  worker whose lease expired mid-run cannot come back and overwrite whoever redid the
  work. A zombie sweep — mounted on ordinary read traffic, since a free-tier host has no
  scheduler — fails missions that exhausted their attempts while still `claimed`.
- **Schema and migrations.** `disciples` and `missions` tables; numbered `.sql`
  migrations applied on boot behind a `pg_advisory_lock`; `python -m sect.core.db
  migrate` / `status`.
- **The server** (`sect.core`, `the-sect[core]`): a FastAPI + asyncpg service with the
  `/v1` REST API and an unauthenticated `/health`. Raw SQL, no ORM — every statement is a
  named constant in `core/sql.py`.
- **The client SDK** (`sect.client`): `Disciple` and `SectMaster`, retry-safe `claim` and
  `complete`, and `run_once(handler)` — the whole body of a scheduled worker. Depends on
  `httpx` and `pydantic` and nothing else.
- **The CLI** (`sect`, `the-sect[cli]`).
- **Cultivation realms** — a maturity tier per disciple, granted by the master rather
  than claimed.
- Stdlib `.env` loading across the server, CLI, and client, with the test suite isolated
  from a developer's `.env`.
- `Dockerfile`, a Render blueprint, and CI (ruff + pytest on Python 3.11 and 3.12 against
  a real `postgres:16`).
- Docs: the architecture design record, a language-agnostic wire-protocol reference, and
  a "writing a disciple" guide.

[Unreleased]: https://github.com/bijay-odyssey/the-sect/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/bijay-odyssey/the-sect/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/bijay-odyssey/the-sect/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bijay-odyssey/the-sect/releases/tag/v0.1.0
