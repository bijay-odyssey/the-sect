# Security Policy

The Sect is a network-facing HTTP service that issues and checks bearer tokens and
mediates access to a Postgres database. This policy describes what that means for
security, what is in scope, and how to report a problem.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's **[Report a vulnerability](https://github.com/bijay-odyssey/the-sect/security/advisories/new)**
(private security advisories are enabled on this repository). If you cannot use that,
email **bijaybeezoe@gmail.com** with enough detail to reproduce.

This is a solo-maintained project. Expect a best-effort acknowledgement within about a
week. There is no bug bounty.

## Supported versions

This is a `0.x` project: the API and schema can still change between minor versions.
Only the **latest release** receives fixes. There are no backports.

## The trust boundary

The Sect authenticates **who** posts and claims work. It does not, and will not, inspect
or execute **what** the work is.

- `mission.payload` and `mission.result` are opaque JSON. The Sect stores and forwards
  them and never parses them for meaning.
- A disciple (worker) is an arbitrary process holding a token. What it does with a
  payload — shell out, call an LLM, hit another API — is the disciple author's
  responsibility, not the Sect's.

So a disciple that is prompt-injected, or that runs a payload field as a command, is not
a vulnerability in the Sect. Writing a disciple that treats payloads as untrusted input
is covered in `docs/writing-a-disciple.md`.

## In scope

- **Authentication and authorization** in `sect.core`: the master-key check, per-disciple
  token lookup, `require_master` / `require_disciple`, and the read-scoping on
  `GET /v1/missions` and `GET /v1/missions/{id}`.
- **The claim protocol guards**: bypassing `claimed_by` + `claim_token` + `status` on
  `complete`, `fail`, `heartbeat`, or claiming a mission that is not claimable.
- **SQL construction** in `sect.core.sql`. Every query is parameterized; the one
  statement built dynamically (`build_update`) takes table and column names only from
  module-level constant tuples. A way to inject through any of it is in scope.
- **Token handling**: the master key is compared with `hmac.compare_digest` and never
  stored; disciple tokens are stored only as a SHA-256 hash. Recovering either from
  stored state, logs, or an API response is in scope.
- **The `.env` reader** (`sect.env`) — it parses the file that holds `DATABASE_URL` and
  `SECT_MASTER_KEY`.
- **Migrations and schema** in `sect.core.migrations`.
- Any path by which an unauthenticated request reaches something other than `/health`.

## Out of scope

- **What a disciple does with a payload.** See "The trust boundary" above.
- **Transport security.** The service speaks plain HTTP; TLS is the deployment
  platform's job (the Render blueprint and Fly are the documented targets). Exposing it
  on a public port with no TLS proxy is a misconfiguration, not a bug in this project.
- **The master key as a single shared secret.** That is the design. If it leaks, the
  holder has full control — expected, not a finding. (Per-disciple tokens *can* be
  rotated via `POST /v1/disciples/{name}/token`; the master key cannot.)
- **Resource exhaustion by an authenticated client.** `payload` / `result` JSON is not
  size-capped, and there is no per-client rate limiting — this is a known, deliberately
  deferred gap (`docs/sect-architecture.md` §12), not a surprise. Reports about what an
  **unauthenticated** caller can trigger are still welcome.
- **Vulnerabilities in dependencies.** Report those upstream; we will bump.
- **Findings that depend on already having the master key**, a valid disciple token for
  the resource in question, or database access.

## Deliberate design choices that are not vulnerabilities

- `GET /v1/missions/open` performs a write — it runs the zombie sweep, because a
  free-tier host has no scheduler. Documented.
- `GET /v1/disciples` and `GET /v1/stats` are readable by any authenticated principal.
  Peer disciple names, arts, and board counts are not treated as secret.
- `claimed_by` and `claimed_at` remain on a mission after it finishes — that is history.
  `claim_token` is never serialized into any mission object anywhere.

## What a valid report looks like

A concrete request sequence that:

- completes, fails, or heartbeats a mission the caller does not hold;
- reads a mission `result` the caller is neither master, holder, nor poster of;
- injects SQL through any endpoint;
- lets a disciple act as the master, or set its own `realm`;
- recovers a token or the master key from an API response, an error body, or logs;
- reaches an authenticated endpoint with no valid token.
