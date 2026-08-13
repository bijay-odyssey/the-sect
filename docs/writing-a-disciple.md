# Writing a disciple

`examples/disciple-scribe/` is a complete, working disciple. This walks through it, because the
fastest way to a second disciple is to copy the first one and change two things.

The SDK surface itself is documented in [sect-architecture.md](sect-architecture.md) §8, and the
raw wire contract in [protocol.md](protocol.md) if the disciple is not written in Python. Neither
is needed to follow this.

---

## What a disciple is

A process with a token and outbound HTTPS. It wakes on a schedule, tells the Sect it exists, takes
one mission matching its art, does the work, reports back, and exits.

It has no inbound port, no uptime requirement, no local state that has to survive a restart, and
no database access. That is what lets the reference deployment be a GitHub Actions cron job on the
free tier rather than a server.

---

## The five steps

1. Copy `examples/disciple-scribe/` into a new repository.
2. Change `ART` and `handle()`.
3. Register the disciple once, with the master key, and keep the token it prints.
4. Put `SECT_URL` and `SECT_TOKEN` in the repository's Actions secrets.
5. Push. The cron workflow is already there.

Everything below is detail on steps 2 and 3.

---

## Walking `main.py`

### The art it claims

```python
ART = "summarize"
```

One string. It has to match the `required_art` on missions you want this disciple to pick up, and
it is the only routing the Sect does. A disciple can declare several arts — `arts=["summarize",
"transcribe"]` — but one is the common case, and a disciple can only ever poll for arts it has
registered.

### The work

```python
def summarize(text: str, sentence_count: int = 2) -> dict[str, object]: ...
```

The scribe's actual job: an extractive summariser in about fifteen lines of standard library. It
is deliberately unclever and dependency-free, to make the point that the Sect does not care. Swap
it for an LLM call, a shell out to `ffmpeg`, or an HTTP request to something else entirely and
nothing else in the file changes.

### The handler

```python
def handle(mission: Mission) -> dict[str, object]:
    text = mission.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PermanentFailure("payload.text must be a non-empty string")
    return summarize(text, int(mission.payload.get("sentences", 2)))
```

This is the only function most disciples need to write. Three rules govern it:

**What you return becomes the result.** Anything JSON-serialisable. It lands in `mission.result`
and comes back from `GET /v1/missions/{id}`.

**What you raise becomes a retryable failure.** The exception type, message, and full traceback
are reported as the mission's `error`, and the mission goes back on the board with a backoff. It
will be tried again until `max_attempts` runs out.

**Except `PermanentFailure`, which is terminal.** Raise it when retrying cannot possibly help — a
malformed payload, an unsupported format, input that will be exactly as broken in ten minutes.
Without it a bad payload burns the whole retry budget proving a point. Note what the scribe does
*not* use it for: a timeout, a 502 from an upstream, a rate limit. Those are ordinary exceptions,
because those get better on their own.

### The body

```python
with Disciple(
    name=os.environ.get("SECT_DISCIPLE_NAME", "scribe"),
    arts=[ART],
    display_name="The Scribe",
    description="Turns long documents into short ones.",
    repo_url="https://github.com/YOUR-USERNAME/disciple-scribe",
    agent_version=os.environ.get("GITHUB_SHA", "local")[:12],
) as disciple:
    mission = disciple.run_once(handle)
```

`run_once` is the whole lifecycle: announce, claim one mission, run the handler, report success or
failure, return the finished mission — or `None` if the board was empty.

`base_url` and `token` are not passed because they default to `$SECT_URL` and `$SECT_TOKEN`, which
is what the workflow provides.

`agent_version` is worth setting. It costs nothing, it is `GITHUB_SHA` for free in CI, and
`sect disciples` then shows you which build did the work — which is the first thing you want when
a disciple starts behaving oddly.

### Exit codes

```python
return 1 if mission.status == "failed" else 0
```

A *retryable* failure exits 0. The mission is back on the board and will be picked up again;
nothing needs a human. Only a terminal failure exits non-zero and turns the Actions run red,
because that is the case where the mission is dead and nobody will look unless something goes red.

---

## Registering it

Once, from a machine with the master key:

```console
$ export SECT_URL=https://sect-core.onrender.com
$ export SECT_MASTER_KEY=...
$ sect disciple create scribe --art summarize --repo https://github.com/you/disciple-scribe
scribe admitted at realm qi-condensation

  SECT_TOKEN=sect_d_EXAMPLE_TOKEN_NOT_A_REAL_ONE_xxxxxxxxxxxxx

This token is shown once and stored only as a hash. Put it in the disciple repo's secrets now; if you lose it, rotate.
```

The warning is literal — only a SHA-256 of that token is stored, so there is no way to read it
back. If it is lost, `sect disciple rotate scribe` issues a new one and invalidates the old.

Then in the disciple's repository: `SECT_TOKEN` as an Actions **secret**, `SECT_URL` as an Actions
**variable** (it is not sensitive, and having it visible makes it easier to see which Sect a
disciple is pointed at).

---

## The schedule

`.github/workflows/cultivate.yml` needs no edits beyond the concurrency group name. Two things
about it are deliberate:

```yaml
concurrency:
  group: disciple-scribe
  cancel-in-progress: false
```

Two disciples claiming two different missions is fine and expected. Two *runs of the same worker*
overlapping usually is not, so runs queue instead.

```yaml
timeout-minutes: 10
```

Comfortably longer than the mission's lease. If a runner is killed while the Sect still believes
the lease is live, the mission sits claimed until the lease expires — correct, but slower than
necessary. Keeping the job timeout above the lease means the disciple usually gets to report a
failure properly instead.

Two GitHub behaviours to expect rather than debug:

- Scheduled workflows are **best-effort** and can be delayed by many minutes at peak. This is why
  missions carry a priority and are recovered by leases rather than by a punctual poll.
- Scheduled workflows are **disabled after 60 days** of repository inactivity. If a disciple goes
  quiet, check that before suspecting the Sect.

---

## Running it locally first

Worth doing before pushing anything.

```console
$ export SECT_URL=http://127.0.0.1:8000
$ export SECT_MASTER_KEY=...
$ sect mission post "Summarize this" --art summarize \
    --payload '{"text": "One two three. Four five six. Seven eight."}'
posted 07e050c5-ae4d-4d16-9c14-1657b4e42800  (summarize, open)

$ export SECT_TOKEN=sect_d_...
$ python main.py
completed: Summarize this  [07e050c5-ae4d-4d16-9c14-1657b4e42800]
{
  "summary": "One two three. Four five six.",
  "words": 8,
  "sentences": 3,
  "keywords": ["one", "two", "three", "four", "five"]
}
```

Run it again with an empty board and it exits 0 quietly:

```console
$ python main.py
No open missions requiring 'summarize'. Returning to cultivation.
```

---

## When it does not work

**`No open missions requiring 'x'` when a mission is clearly there.** The arts do not match.
Check `sect missions` against the disciple's registered arts in `sect disciples` — a disciple can
only be dealt work for arts it declared, and `PUT /v1/disciples/me` overwrites the declared list
on every wake-up, so the deployed code is the source of truth.

**`403 forbidden_art`.** The same mismatch, but explicit: something asked for an art the disciple
never registered.

**`409 not_mission_holder` on complete.** The lease expired mid-run and someone else took the
mission. `detail.reason` says which flavour. Either the work is slower than `lease_seconds` — post
those missions with a longer lease, or call `heartbeat` — or the runner was suspended.

**The first request of the day takes a minute.** That is the host waking from idle, and it is
expected. The SDK waits 90 seconds and retries; do not add a keep-alive pinger to work around it.

**`401 disciple_inactive`.** Somebody ran `sect disciple deactivate`. `sect disciple reinstate`
undoes it.
