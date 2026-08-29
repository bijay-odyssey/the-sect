# Contributing to The Sect

Thanks for looking. This project is small on purpose and intends to stay that way, so it
helps to know what it is before you open a PR.

## Before you start

- **Claim the issue.** Comment on it to say you're taking it, and check nobody already
  has. Two people fixing the same thing in parallel is a waste of an evening.
- **One logical change per PR.** A bug fix and a refactor are two PRs.
- Branch from `main`. PRs target `main`.
- If the issue is a design question rather than a defined task, argue it in the issue
  before writing code.

## The shape of the codebase

Everything is under `src/sect/`:

| Path | What it is | Who installs it |
|---|---|---|
| `models.py` | The wire contract — every request and response as a Pydantic model. Imported by **both** the client and the server, so the two cannot drift. | everyone |
| `client.py` | The SDK. Depends on `httpx` + `pydantic` and **nothing else**. Two classes split by privilege: `Disciple` (a worker) and `SectMaster` (admin). | `pip install the-sect` |
| `cli.py` | The `sect` command (Typer). | `the-sect[cli]` |
| `realms.py`, `errors.py`, `env.py` | Ordered cultivation realms; the SDK exception hierarchy; a stdlib `.env` reader. | everyone |
| `core/` | The server. FastAPI + asyncpg. **A disciple never imports this.** | `the-sect[core]` |
| `core/sql.py` | Every SQL statement the server issues, as a named constant. | — |
| `core/migrations/000N_*.sql` | Numbered schema migrations, applied on boot behind a `pg_advisory_lock`. | — |

`tests/` runs against a **real PostgreSQL**. The guarantees under test — row locks,
predicate re-evaluation under `READ COMMITTED`, `FOR UPDATE SKIP LOCKED` — are database
behaviours, not application behaviours, and there is no in-memory substitute.
`examples/` and `peak-template/` are standalone and are not collected by `pytest`
(`testpaths = ["tests"]`).

### The rule the project exists to keep

**A mission's state never changes via read-then-write.** Every transition (`claim`,
`complete`, `fail`, `heartbeat`, `cancel`, the sweep) is one conditional `UPDATE` whose
`WHERE` clause is the guard. The winner of a race gets a row back; everyone else matches
zero rows and gets a `409`. There is no check-then-act window because there is no
separate check. If you find yourself writing a `SELECT` to decide whether to run an
`UPDATE` on the claim path, stop — that is the bug this codebase is built to not have.

Related invariants, all load-bearing:

- **Postgres is the only coordination primitive.** No queue, no broker, no Redis, no
  application-level locks.
- **Raw SQL, no ORM.** The correctness of this project *is* the SQL; it stays reviewable
  in one file.
- **`READ COMMITTED`.** Raising the isolation level turns "quietly match zero rows" into
  a serialization error the app must catch.
- **One clock.** Every `now()` is evaluated by Postgres. Never compute a deadline or
  compare a timestamp client-side.
- **The base install is `httpx` + `pydantic`.** A worker's whole dependency tree. Adding
  to `[project.dependencies]` needs a strong argument in the issue first.
- **Migrations are append-only.** Add `000N_name.sql`; never edit one that has shipped.

## Running it locally

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
make install     # pip install -e ".[core,cli,dev]"
make pg          # a throwaway Postgres on :5432 (or bring your own — see below)
make check       # ruff check + ruff format --check + pytest, i.e. what CI runs
```

The test suite expects `postgresql://postgres:postgres@localhost:5432/sect_test`.
Point it elsewhere with `TEST_DATABASE_URL`. Other targets: `make run` (server with
reload), `make test`, `make lint`, `make fmt`. Migrations outside the test suite:
`python -m sect.core.db migrate` / `status` (reads `DATABASE_URL`).

## What CI checks

CI runs on every pull request. A PR cannot merge until all of it is green:

| Check | Command | Notes |
|---|---|---|
| Lint | `ruff check .` | rule set `E, F, W, I, UP, B, SIM, RUF` |
| Format | `ruff format --check .` | run `ruff format .` (or `make fmt`) before pushing |
| Tests | `pytest -q` | on **Python 3.11 and 3.12**, against `postgres:16` |
| Image | `docker build .` | must still build if you touched the `Dockerfile` or `[core]` deps |

## What "done" looks like

- CI is green.
- The change has a test that **fails without it**. There is no unit-test shortcut around
  the database here.
- If it changes the wire contract, `models.py` and `docs/protocol.md` move together.
- If it changes a state transition, the constant in `core/sql.py` changes and a
  concurrency test in `tests/` covers the new behaviour.
- The PR body has a real `Closes #NN` line.

## Declined on sight

These aren't personal; they're the project's whole point:

- Splitting a mission state transition into `SELECT`-then-`UPDATE`, or moving the guard
  out of the SQL `WHERE` clause.
- Adding a queue, broker, message bus, Redis, or Celery.
- Adding an ORM.
- Raising the database isolation level above `READ COMMITTED`.
- A new dependency in the **base** install (anything beyond `httpx` + `pydantic`).
- Trusting a client clock for any deadline.
- An async SDK. The SDK is sync by design — a cron job is a sync script. If this should
  change it's a design discussion, not a drive-by PR.
- A new auth scheme beyond the master key and hashed per-disciple tokens, without a
  design issue first.

## Commit and PR conventions

- Commits and PRs describe the change and why it matters.
- One reviewer-visible logical change per PR.
- If a change needs a bulk reformat or a line-ending sweep, do that in its **own**
  commit and add the SHA to `.git-blame-ignore-revs`.

By contributing you agree your work is licensed under [Apache-2.0](LICENSE), the
project's licence.
