# Peak template

A **peak** is a specialty in the Sect: a name, a set of arts, and one or more workers
("disciples") that do the work. This directory is a complete, working peak worker with
the handler left blank. Copy it, fill in two things, deploy.

A peak does **not** need to be an always-on service. It is a collection of disciples that
share an identity and a specialty. Like any disciple, a worker here can be a GitHub
Actions cron, a systemd timer, a Raspberry Pi — anything that runs Python on a schedule
and has outbound HTTPS. It never talks to Postgres.

---

## The five steps

1. **Copy** `peak-template/` into a new repository.
2. **Edit `peak_config.yaml`** — `name`, `arts`, `description`.
3. **Edit `handle()` in `main.py`** — that one function is your peak's work.
4. **Register** the peak and one disciple with your Sect (below), and put the token in
   the repo's Actions secrets.
5. **Push.** `.github/workflows/cultivate.yml` runs it every 15 minutes.

---

## What you edit

### `peak_config.yaml`

```yaml
name: scraping-peak
display_name: Web Scraping Peak
description: Fetches and parses the open web.
arts: [web_scraping, html_parsing]
disciple_name: scraping-peak-worker
```

`main.py` reads this file directly, so it is the single source of truth. `arts` is the
routing key: a mission's `required_art` must be one of these for a worker here to be
dealt it.

### `handle(mission)` in `main.py`

```python
def handle(mission: Mission) -> object:
    return do_the_work(mission.payload)
```

Three rules:

- **What you return becomes `mission.result`** — anything JSON-serialisable.
- **What you raise is a retryable failure** — the mission goes back on the board with a
  backoff and is retried until `max_attempts` runs out. Use this for a timeout, a 502, a
  rate limit: things that get better on their own.
- **Except `PermanentFailure`, which is terminal** — raise it when a retry cannot
  possibly help: a malformed payload, an unsupported format.

Nothing else in `main.py` needs to change.

---

## Registering it

Once, from a machine with the master key (`SECT_URL`, `SECT_MASTER_KEY` set):

```bash
# 1. Register the peak.
curl -sS -X POST "$SECT_URL/v1/peaks" \
  -H "Authorization: Bearer $SECT_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"scraping-peak","display_name":"Web Scraping Peak",
       "arts":["web_scraping","html_parsing"]}'

# 2. Register a worker in it. Prints a SECT_TOKEN once — store it now.
curl -sS -X POST "$SECT_URL/v1/disciples" \
  -H "Authorization: Bearer $SECT_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"scraping-peak-worker","arts":["web_scraping","html_parsing"],
       "peak":"scraping-peak"}'
```

Then in the worker's repository: `SECT_TOKEN` as an Actions **secret**, `SECT_URL` as an
Actions **variable**. The worker re-asserts its peak and arts on every run, so the
deployed `peak_config.yaml` is always the source of truth.

> A `sect peak` CLI group is planned; until then the API is the way in.

A peak is **not a wall**. Tagging a mission with your peak makes your workers see it
first, but any disciple whose arts match can still claim it. Specialization is for
excellence, not exclusion.

---

## Running it locally first

```bash
pip install -r requirements.txt          # or: pip install -e ..  (inside the-sect repo)
export SECT_URL=http://127.0.0.1:8000
export SECT_MASTER_KEY=...

# post a mission your peak can do
curl -sS -X POST "$SECT_URL/v1/missions" \
  -H "Authorization: Bearer $SECT_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"title":"try it","required_art":"web_scraping","payload":{"url":"https://example.com"}}'

export SECT_TOKEN=sect_d_...
python main.py
```

Run `pytest` in this directory to exercise `handle()` on its own — no Sect, no network.
See `tests/test_peak.py`.
