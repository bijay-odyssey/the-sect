# The Sect

A small task-orchestration framework for one person running a lot of little services.

Work gets posted to a shared board with a required skill tag. Independent workers poll for
matching work, claim it atomically, do it however they like, and report back. That is the whole
idea.

It is deliberately not Temporal, Airflow, or Dapr. One HTTP service, one Postgres, a handful of
tables, no queue and no broker — sized for a solo developer coordinating a dozen small repos, and
cheap enough to run on free tiers.

It uses cultivation-sect vocabulary throughout, because naming things is free and this was more
fun than `worker` and `job`:

| | |
|---|---|
| **The Sect** | the service and its database |
| **Disciple** | a worker — its own repo, its own deploy, its own schedule |
| **Art** | a skill tag. Disciples declare them; missions require one |
| **Mission Hall** | the task board |
| **Cultivation realm** | a maturity tier per disciple, granted rather than claimed |
| **Peak** | a specialty a disciple can join (`v0.2`). A routing hint, never a wall |

---

## A disciple

```python
from sect import Disciple


def handle(mission):
    return {"words": len(mission.payload["text"].split())}


Disciple(name="scribe", arts=["summarize"]).run_once(handle)
```

`run_once` announces the disciple, claims one matching mission, runs the handler, reports the
result, and exits. Point it at a Sect with `SECT_URL` and `SECT_TOKEN` and that is a complete
worker — the reference deployment is that file on a GitHub Actions cron, with no always-on server
anywhere.

```console
$ pip install the-sect
```

The base install is the client: `httpx` and `pydantic`, nothing else. A disciple never talks to
Postgres.

> **Not on PyPI yet.** Until a release is published, install from git:
> `pip install "the-sect @ git+https://github.com/bijay-odyssey/the-sect"`. The `pip install
> the-sect` form above is what it will be once published.

---

## Running the Sect itself

```console
$ pip install "the-sect[core] @ git+https://github.com/bijay-odyssey/the-sect"
$ export DATABASE_URL=postgresql://...
$ export SECT_MASTER_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
$ uvicorn sect.core.app:create_app --factory
```

Migrations run on boot behind an advisory lock, so there is no release phase. A `Dockerfile` and a
Render blueprint are included; it is built to sit on a free tier that spins down when idle.

Then, with `pip install "the-sect[cli] @ git+https://github.com/bijay-odyssey/the-sect"`:

```console
$ sect disciple create scribe --art summarize
$ sect mission post "Summarize the week" --art summarize --payload '{"text": "..."}'
$ sect missions
$ sect stats
```

---

## Claiming is the interesting part

Two disciples must never get the same mission. That is enforced by a single conditional `UPDATE`
whose `WHERE` clause is the guard — no read-then-write, no application-level locking:

```sql
UPDATE missions SET status = 'claimed', ...
WHERE id = $1 AND (status = 'open' OR lease expired OR already mine)
RETURNING *;
```

The winner gets a row; everyone else matches zero rows and gets a `409`. Under PostgreSQL's
default READ COMMITTED, a losing transaction blocks on the row lock, re-evaluates the predicate
against the newly committed row, and drops out. There is no window between the check and the write
because there is no separate check.

Disciples die — a CI runner gets killed, a job times out — so every claim carries a lease. When it
expires the mission becomes claimable again, and a per-claim token means the disciple that
vanished cannot come back later and overwrite the result of whoever redid the work.

`tests/test_claim_atomicity.py` fires twenty simultaneous claims at one mission and asserts exactly
one winner. A naive read-then-write version hands the same mission to several of them.

---

## Peaks (v0.2)

A **peak** is a specialty a growing collection of disciples can organise around — `scraping-peak`,
`llm-peak`, `backup-peak`. Register one, point disciples at it, and those disciples are offered
their peak's work first. That is the *only* effect: a mission tagged with a peak is still
claimable by any disciple whose arts match. Specialization is for excellence, not exclusion.

```console
$ curl -X POST $SECT_URL/v1/peaks -H "Authorization: Bearer $SECT_MASTER_KEY" \
    -d '{"name":"scraping-peak","display_name":"Web Scraping Peak","arts":["web_scraping"]}'
$ sect disciple create scraper --art web_scraping --peak scraping-peak
```

Completed missions earn a disciple **contribution points**; `reputation` is
`points × success_rate`. Both follow the disciple, not the peak. [`peak-template/`](peak-template/)
is a fork-and-fill-in starting point for a new peak.

---

## Docs

- [docs/sect-architecture.md](docs/sect-architecture.md) — the full design: schema, every
  endpoint, the SDK, and why the claim is correct. §16 is the v0.2 Peak System addendum.
- [docs/protocol.md](docs/protocol.md) — the wire contract, for writing a disciple in something
  other than Python.
- [docs/writing-a-disciple.md](docs/writing-a-disciple.md) — a walk through the worked example in
  `examples/disciple-scribe/`.
- [peak-template/](peak-template/) — copy this to start a new peak.

## Contributing

Issues and PRs are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has the architecture in a page,
the invariants a PR is judged on, and how to run the tests locally; there are
[good first issues](https://github.com/bijay-odyssey/the-sect/labels/good%20first%20issue) open.

Thanks to everyone who has [contributed](https://github.com/bijay-odyssey/the-sect/graphs/contributors).

## Status

v0.2: peaks, a per-disciple contribution ledger, structured JSON logs. Still honestly scoped —
no dashboard and no queue transport yet. The architecture doc records what was deliberately left
out and where the seams for it are.

Requires Python 3.11+ and PostgreSQL 13+. Apache-2.0.
