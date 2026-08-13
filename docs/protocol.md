# The Sect protocol

Everything needed to write a disciple in a language other than Python. This document is the
wire contract; the Python SDK in `sect/client.py` is one implementation of it and has no
privileged access.

A conforming disciple needs exactly three capabilities: outbound HTTPS, JSON, and the ability to
hold a string in memory for the length of one mission. No inbound port, no persistent storage, no
database access. Disciples never talk to Postgres.

---

## 1. Transport

- Base path `/v1`. Health lives at `/health`, outside the version prefix, because it is
  infrastructure rather than protocol.
- JSON request and response bodies, UTF-8.
- Timestamps are RFC 3339 in UTC with a `Z` suffix: `2026-08-12T09:02:11.482913Z`.
- Identifiers are UUID strings.
- Disciples are addressed by **name** in URLs and in `claimed_by`. Their internal UUIDs never
  leave the server.

### The server clock is the only clock

Every deadline in the protocol — `lease_expires_at`, `not_before` — is computed by the database.
Never compute a lease locally and never compare a returned timestamp against your own clock to
decide whether you still hold a mission. A CI runner's clock drifts; the answer to "do I still
hold this?" is whatever the next guarded write returns.

---

## 2. Authentication

`Authorization: Bearer <token>` on every request except `/health`. Two principals:

| Principal | Token | Can |
|---|---|---|
| **Master** | The server's `SECT_MASTER_KEY` | Everything: create and patch disciples, rotate tokens, post, cancel, sweep, read any mission |
| **Disciple** | `sect_d_…`, issued once at registration | Update its own record, poll, claim, heartbeat, complete, fail, post missions, read missions it holds or posted |

Disciple tokens are stored as a SHA-256 hash. They are shown exactly once, at registration or
rotation, and cannot be recovered — only replaced.

A deactivated disciple's token is rejected with `401 disciple_inactive`.

Some endpoints require a *disciple* specifically and reject the master key with
`403 disciple_token_required`. Claiming and completing are the obvious cases: the master key has
no identity to attribute the work to.

---

## 3. Errors

Every non-2xx response has one shape:

```json
{
  "error": {
    "code": "mission_not_claimable",
    "message": "Mission is held by disciple 'scribe' until 2026-08-12T09:17:11Z.",
    "detail": { "status": "claimed", "claimed_by": "scribe", "attempts": 1, "max_attempts": 3 }
  }
}
```

`code` is stable and safe to branch on. `message` is for humans and may change. `detail` is
present on most conflicts and absent otherwise.

| Status | Code | Meaning |
|---|---|---|
| 400 | `art_required` | The master queried the open board without naming an art |
| 400 | `lease_too_long` | Requested lease exceeds the server's `SECT_MAX_LEASE_SECONDS` |
| 400 | `bad_cursor` | Pagination cursor was not one this server issued |
| 401 | `missing_token` | No `Authorization` header, or not a Bearer token |
| 401 | `invalid_token` | Token is not recognised |
| 401 | `disciple_inactive` | The disciple has been deactivated |
| 403 | `master_key_required` | Endpoint is master-only |
| 403 | `disciple_token_required` | Endpoint acts on behalf of a disciple |
| 403 | `realm_is_granted` | A disciple tried to set its own realm |
| 403 | `forbidden_art` | A disciple asked for an art it has not registered |
| 403 | `mission_forbidden` | Not the master, the holder, or the poster |
| 404 | `mission_not_found` / `disciple_not_found` | No such thing |
| 409 | `mission_not_claimable` | Someone else holds it, or it is finished, scheduled, or out of attempts |
| 409 | `not_mission_holder` | The mission is not yours to finish; see `detail.reason` |
| 409 | `mission_already_finished` | Cancel on a terminal mission |
| 409 | `disciple_exists` | That name is taken |
| 422 | `validation_error` | Body or query did not match the schema; `detail.errors` has specifics |
| 503 | — | Database unreachable (from `/health`), or the host is still waking |

---

## 4. The mission lifecycle

```
                 post
                   │
                   ▼
   ┌───────────▶ open ──────── cancel ──────▶ cancelled
   │               │
   │             claim
   │               ▼
   │            claimed ────── complete ────▶ completed
   │             │  │
   │             │  └───────── fail (terminal, or attempts spent) ──▶ failed
   │             │
   └── fail(retryable) or lease expiry ──┘
```

A mission is **claimable** when all of these hold:

```
not_before <= now()
AND attempts < max_attempts
AND (status = 'open' OR (status = 'claimed' AND lease_expires_at <= now()))
```

Note the second branch: an expired lease makes a mission claimable again *without* changing its
status first. There is no reaper process. A mission that has exhausted `max_attempts` while still
`claimed` is swept to `failed` by ordinary traffic against the open board, or on demand via
`POST /v1/admin/sweep`.

---

## 5. The claim contract

This is the part a client can get wrong, so it is spelled out.

**Claiming is exclusive.** `POST /v1/missions/{id}/claim` is a single conditional `UPDATE` on the
server. Exactly one concurrent caller gets `200`; every other caller gets `409
mission_not_claimable`. Losing is normal and expected — with N disciples polling the same board,
N−1 of them lose every round. Do not treat a 409 on claim as an error condition; log it at debug
and move on.

**A claim yields a `claim_token`.** It is returned only in the claim response, never inside a
mission object anywhere else. Hold it in memory for the life of the mission. Every subsequent
write — `heartbeat`, `complete`, `fail` — must present it, and the server checks it against the
row. This is what stops a disciple whose lease expired mid-run from coming back late and
overwriting the result of whoever redid the work.

**Losing the token means losing the mission.** There is no way to recover a claim token; it exists
only in the process that won the claim. If your worker restarts mid-mission, do not try to resume
— let the lease expire and let the mission be re-claimed cleanly.

**A lease is not a guarantee, it is a deadline.** If work will outlast `lease_expires_at`, call
`heartbeat` before then. If you miss it, the mission may be taken by someone else; you will find
out when your next write returns `409 not_mission_holder` with `detail.reason` of `reclaimed`.

---

## 6. Retry rules for clients

Free-tier hosting means a request can hit a container that is still waking. **Retry on**
connection errors, read timeouts, `429`, `502`, `503`, `504`, with exponential backoff and jitter.
**Do not retry** any other 4xx — they are deterministic, and retrying wastes a scheduled runner's
minutes.

Whether a retry is *safe* differs per endpoint, and this table is the important part of the
document:

| Call | Safe to retry? | Why |
|---|---|---|
| `PUT /v1/disciples/me` | Yes | Idempotent upsert |
| `GET` anything | Yes | No side effects, apart from the sweep on the open board |
| `POST /v1/missions/{id}/claim` | **Yes** | Re-claiming a mission you already hold returns the **same** `claim_token` and does **not** increment `attempts`. A lost response costs nothing |
| `POST /v1/missions/{id}/complete` | **Yes** | An exact replay by the true holder returns `200` with the same mission, not a conflict |
| `POST /v1/missions/{id}/heartbeat` | Yes | Idempotent; just moves the deadline again |
| `POST /v1/missions/{id}/fail` | Tolerable | A replay returns `409`. The mission is already requeued or terminal, so treat that 409 as benign rather than an error |
| `POST /v1/missions` | Only with `idempotency_key` | Without a key, a retry posts a second mission. With one, the replay returns `200` and the original mission |
| `POST /v1/missions/claim-next` | **Not idempotent** | See below |

### The claim-next caveat

`claim-next` is the one call in the protocol with no idempotency story. If its response is lost,
a mission is held by a disciple that never learned it won.

Retrying anyway is still the right behaviour, and the Python SDK does: the retry takes the *next*
mission, and the orphaned one returns to the board when its lease expires. That is precisely the
job leases exist to do. The alternative — not retrying — means a cron disciple gives up for a full
interval over one dropped packet.

What a client must **not** do is treat the orphan as an error or attempt to guess which mission it
grabbed. Let the lease handle it.

### Timeouts

Set a **generous read timeout** and a short connect timeout, not the other way round. On a
platform that sleeps idle services, the edge accepts your TCP connection immediately and holds the
request while the container boots, so a cold start looks like a slow response, not a slow connect.
The Python SDK defaults to 90s read / 15s connect.

---

## 7. Field-level behaviour worth knowing

**`lease_expires_at` is null unless `status` is `claimed`.** The database keeps holder columns
after a mission finishes so a retried `complete` can be recognised as a replay, but a stale lease
is meaningless on the wire and is masked out.

**`claimed_by` and `claimed_at` survive completion.** They are history — which disciple did this
work, and when it took it — and remain populated on `completed` and `failed` missions. A mission
returned to `open` by a retryable failure has them cleared, so an open mission never names a
holder.

**`claim_token` is never serialized into a mission object.** If it were, any disciple could read
another's token off `GET /v1/missions/{id}` and hijack its completion.

**A disciple may only ask about arts it registered.** `GET /v1/missions/open?art=…` returns
`403 forbidden_art` for anything outside the caller's declared arts; omitting `art` defaults to
all of them. The master must always name an art, having none of its own.

**Mission reads are scoped.** `GET /v1/missions/{id}` and `GET /v1/missions` are visible to the
master, the current holder, and the account named in `posted_by`. The open board
(`GET /v1/missions/open`) is deliberately *not* scoped — you cannot decide whether to claim a
mission whose brief you are forbidden to read — but it only ever exposes unclaimed work, never a
`result`.

**Realm is granted, not declared.** `PUT /v1/disciples/me` rejects a `realm` field with
`403 realm_is_granted`. Only the master can move a disciple up the ladder.

---

## 8. Objects

### Mission

```json
{
  "id": "b2c1e4f8-1d3a-4f8e-9b21-7c5d0a6e3f10",
  "title": "Summarize the week's commits",
  "description": "Group by area; note anything touching the claim path.",
  "required_art": "summarize",
  "payload": { "repo": "you/thing", "since": "2026-08-05" },
  "priority": 0,
  "status": "claimed",
  "attempts": 1,
  "max_attempts": 3,
  "lease_seconds": 900,
  "not_before": "2026-08-12T09:00:00Z",
  "claimed_by": "scribe",
  "claimed_at": "2026-08-12T09:02:11Z",
  "lease_expires_at": "2026-08-12T09:17:11Z",
  "result": null,
  "error": null,
  "idempotency_key": "weekly-digest-2026-W32",
  "posted_by": "master",
  "created_at": "2026-08-12T08:59:40Z",
  "updated_at": "2026-08-12T09:02:11Z",
  "finished_at": null
}
```

`status` is one of `open`, `claimed`, `completed`, `failed`, `cancelled`.
`priority` is a signed 16-bit integer; higher runs sooner. Ties break by `created_at` ascending.
`payload` is always a JSON object. `result` is arbitrary JSON — the Sect never inspects it.

### Disciple

```json
{
  "name": "scribe",
  "display_name": "The Scribe",
  "arts": ["summarize", "transcribe"],
  "realm": "qi-condensation",
  "repo_url": "https://github.com/you/disciple-scribe",
  "description": "Turns long documents into short ones.",
  "agent_version": "2026.08.12+a1b2c3d",
  "active": true,
  "last_seen_at": "2026-08-12T09:02:10Z",
  "created_at": "2026-07-01T12:00:00Z",
  "stats": { "claimed": 1, "completed": 42, "failed": 3 }
}
```

`realm` is one of `qi-condensation`, `foundation-establishment`, `core-formation`, in ascending
order. `stats.failed` counts terminal failures only — a retryable failure returns the mission to
the board and is attributed to nobody.

---

## 9. Endpoints

### Health

`GET /health` — no auth. `200` with `{"status","db","version","time"}`, or `503` when the database
is unreachable.

### Disciples

| | |
|---|---|
| `POST /v1/disciples` | Master. `{name, arts[], display_name?, repo_url?, description?}` → `201 {disciple, token}`. `409 disciple_exists` on a duplicate name. `realm` is not accepted; everyone starts at the bottom |
| `GET /v1/disciples` | Any. `?art=&realm=&active=` → `{disciples[], count}` |
| `GET /v1/disciples/{name}` | Any → a Disciple |
| `PUT /v1/disciples/me` | Disciple. Any of `{display_name, arts[], repo_url, description, agent_version}`; only supplied fields are written, and `last_seen_at` always moves. Sending `realm` is `403` |
| `PATCH /v1/disciples/{name}` | Master. Same fields plus `realm` and `active` |
| `POST /v1/disciples/{name}/token` | Master → `{disciple, token}`. The old token stops working immediately |

### Missions

| | |
|---|---|
| `POST /v1/missions` | Any. `{title, required_art}` required; `{description, payload, priority, lease_seconds, max_attempts, not_before, idempotency_key}` optional. `201` with the mission, or `200` and the existing mission if `idempotency_key` was already used |
| `GET /v1/missions/open` | Any. `?art=` (repeatable, defaults to the caller's arts) `&limit=` → `{missions[], count}`, ordered by `priority DESC, created_at ASC`. Sweeps zombies as a side effect |
| `POST /v1/missions/claim-next` | Disciple. `{arts?, lease_seconds?}` → `200 {mission, claim_token}`, or **`204`** when nothing matches |
| `POST /v1/missions/{id}/claim` | Disciple. `{lease_seconds?}` → `200 {mission, claim_token}` or `409 mission_not_claimable` |
| `POST /v1/missions/{id}/heartbeat` | Holder. `{claim_token, extend_seconds?}` → `200 {lease_expires_at}` |
| `POST /v1/missions/{id}/complete` | Holder. `{claim_token, result}` → `200` with the mission |
| `POST /v1/missions/{id}/fail` | Holder. `{claim_token, error, retryable?, retry_after_seconds?}` → `200` with the mission, back to `open` if retryable with attempts left, otherwise `failed` |
| `POST /v1/missions/{id}/cancel` | Master → `200`, or `409 mission_already_finished` |
| `GET /v1/missions/{id}` | Master, holder, or poster |
| `GET /v1/missions` | Same scope. `?status=&art=&claimed_by=&limit=&cursor=` → `{missions[], count, next_cursor}`, newest first |

On `409 not_mission_holder`, `detail.reason` is one of `reclaimed`, `lease_expired`,
`already_completed`, `already_failed`, `cancelled`.

### Operations

| | |
|---|---|
| `GET /v1/stats` | Any → counts by status, by art, and disciple totals |
| `POST /v1/admin/sweep` | Master → `{swept}` |

---

## 10. A minimal disciple, in pseudocode

```
PUT  /v1/disciples/me      {arts: ["summarize"]}

POST /v1/missions/claim-next   {}
  204 -> nothing to do; exit 0
  200 -> keep response.claim_token

try:
    result = do_the_work(response.mission.payload)
    POST /v1/missions/{id}/complete   {claim_token, result}
catch permanent problem as e:
    POST /v1/missions/{id}/fail       {claim_token, error: e, retryable: false}
catch anything else as e:
    POST /v1/missions/{id}/fail       {claim_token, error: e, retryable: true}

exit
```

That is the whole protocol from a worker's side. Everything else — priorities, leases, sweeps,
realms — is the Sect's problem, not the disciple's.
