"""Every SQL statement the Sect issues, named, in one file.

The correctness of this project *is* the SQL, so it lives here rather than scattered
through route handlers: the claim can be read, reviewed and tested in isolation.

The rule that matters: **a mission's state never changes via read-then-write.** Every
transition is one statement whose WHERE clause is the guard. If a statement here ever
grows a companion SELECT that decides whether to run it, the atomicity argument in
docs/sect-architecture.md §6.2 stops holding.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #


def _mission_projection(alias: str) -> str:
    """Columns for a wire-shaped mission, joined to ``disciples AS d`` for the name.

    ``alias`` is always a module-internal constant, never user input.
    """
    a = alias
    return f"""
        {a}.id, {a}.title, {a}.description, {a}.required_art, {a}.payload,
        {a}.priority, {a}.status, {a}.attempts, {a}.max_attempts, {a}.lease_seconds,
        {a}.not_before, d.name AS claimed_by, {a}.claimed_at, {a}.lease_expires_at,
        {a}.result, {a}.error, {a}.idempotency_key, {a}.posted_by,
        {a}.created_at, {a}.updated_at, {a}.finished_at,
        {a}.claimed_by AS claimed_by_id
    """


_MISSION = _mission_projection("m")
# Writes return through a CTE aliased "c"; claim_token is added only where the caller
# is entitled to see it. Mission (extra="ignore") drops it on the way to the wire.
_CLAIMED = _mission_projection("c") + ", c.claim_token"
_WRITTEN = _mission_projection("c")

_DISCIPLE = """
    d.id, d.name, d.display_name, d.arts, d.realm, d.repo_url, d.description,
    d.agent_version, d.active, d.last_seen_at, d.created_at,
    COALESCE(s.claimed, 0)   AS stat_claimed,
    COALESCE(s.completed, 0) AS stat_completed,
    COALESCE(s.failed, 0)    AS stat_failed
"""

# Counting per disciple. `failed` sees only terminal failures: a retryable failure
# clears the holder columns, so it is attributed to nobody.
_DISCIPLE_STATS_JOIN = """
    LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE m.status = 'claimed')   AS claimed,
               count(*) FILTER (WHERE m.status = 'completed') AS completed,
               count(*) FILTER (WHERE m.status = 'failed')    AS failed
        FROM missions AS m
        WHERE m.claimed_by = d.id
    ) AS s ON true
"""

# The claimable predicate, written once. Any statement that decides whether work can be
# picked up uses this and nothing else.
_CLAIMABLE = """
    {a}.not_before <= now()
    AND {a}.attempts < {a}.max_attempts
    AND (
          {a}.status = 'open'
       OR ({a}.status = 'claimed' AND {a}.lease_expires_at <= now())
    )
"""


# --------------------------------------------------------------------------- #
# Disciples
# --------------------------------------------------------------------------- #

SELECT_DISCIPLE_BY_TOKEN_HASH = """
SELECT id, name, arts, realm, active
FROM disciples
WHERE token_hash = $1
"""

INSERT_DISCIPLE = """
INSERT INTO disciples (name, display_name, arts, repo_url, description, token_hash)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (name) DO NOTHING
RETURNING id
"""

SELECT_DISCIPLE = f"""
SELECT {_DISCIPLE}
FROM disciples AS d
{_DISCIPLE_STATS_JOIN}
WHERE d.name = $1
"""

LIST_DISCIPLES = f"""
SELECT {_DISCIPLE}
FROM disciples AS d
{_DISCIPLE_STATS_JOIN}
WHERE ($1::text IS NULL OR $1 = ANY(d.arts))
  AND ($2::text IS NULL OR d.realm = $2)
  AND ($3::boolean IS NULL OR d.active = $3)
ORDER BY d.name
"""

ROTATE_TOKEN = """
UPDATE disciples SET token_hash = $2 WHERE name = $1 RETURNING id
"""

#: Columns a disciple may change about itself. Realm is absent by design.
SELF_UPDATABLE: tuple[str, ...] = (
    "display_name",
    "arts",
    "repo_url",
    "description",
    "agent_version",
)

#: Columns the master may change. This is where ascension happens.
MASTER_UPDATABLE: tuple[str, ...] = (
    "display_name",
    "arts",
    "repo_url",
    "description",
    "realm",
    "active",
)


def build_disciple_update(
    fields: dict[str, Any],
    allowed: tuple[str, ...],
    *,
    touch_last_seen: bool,
) -> tuple[str, list[Any]]:
    """Build a partial UPDATE from explicitly-supplied fields.

    Callers pass ``model_dump(exclude_unset=True)``, so "not mentioned" and "set to
    null" stay distinguishable and a disciple can refresh only its ``agent_version``
    without wiping its description.

    Column names come from ``allowed`` -- a module constant -- and never from the
    request; only values are parameterized.
    """
    assignments: list[str] = []
    args: list[Any] = []
    for column in allowed:
        if column in fields:
            args.append(fields[column])
            assignments.append(f"{column} = ${len(args)}")
    if touch_last_seen:
        assignments.append("last_seen_at = now()")
    if not assignments:
        return "", args

    args.append(None)  # placeholder slot for the WHERE value, filled by the caller
    where_index = len(args)
    sql = f"UPDATE disciples SET {', '.join(assignments)} WHERE id = ${where_index} RETURNING id"
    return sql, args


# --------------------------------------------------------------------------- #
# Missions: create and read
# --------------------------------------------------------------------------- #

INSERT_MISSION = f"""
WITH ins AS (
    INSERT INTO missions (
        title, description, required_art, payload, priority,
        lease_seconds, max_attempts, not_before, idempotency_key, posted_by
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8::timestamptz, now()), $9, $10)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING missions.*
)
SELECT {_WRITTEN}
FROM ins AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
"""

SELECT_MISSION = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
WHERE m.id = $1
"""

SELECT_MISSION_BY_IDEMPOTENCY_KEY = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
WHERE m.idempotency_key = $1
"""

LIST_OPEN_MISSIONS = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
WHERE m.required_art = ANY($1::text[])
  AND {_CLAIMABLE.format(a="m")}
ORDER BY m.priority DESC, m.created_at ASC
LIMIT $2
"""

# $6/$7 carry the read scope: NULL for the master (sees everything), otherwise the
# calling disciple's id and name, so it sees only what it holds or posted.
LIST_MISSIONS = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
WHERE ($1::text IS NULL OR m.status = $1)
  AND ($2::text IS NULL OR m.required_art = $2)
  AND ($3::uuid IS NULL OR m.claimed_by = $3)
  AND ($4::timestamptz IS NULL OR (m.created_at, m.id) < ($4::timestamptz, $5::uuid))
  AND ($6::uuid IS NULL OR m.claimed_by = $6::uuid OR m.posted_by = $7::text)
ORDER BY m.created_at DESC, m.id DESC
LIMIT $8
"""

#: Diagnosis only -- never a guard. Used to explain a 409 after a conditional UPDATE
#: has already matched zero rows.
INSPECT_MISSION = """
SELECT m.status, m.attempts, m.max_attempts, m.not_before, m.lease_expires_at,
       m.claimed_by AS claimed_by_id, d.name AS claimed_by,
       (m.claim_token IS NOT DISTINCT FROM $2::uuid) AS token_matches
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
WHERE m.id = $1
"""


# --------------------------------------------------------------------------- #
# Missions: the state transitions
# --------------------------------------------------------------------------- #

CLAIM_MISSION = f"""
WITH claimed AS (
    UPDATE missions AS m
    SET status     = 'claimed',
        claimed_by = $2::uuid,
        claim_token = CASE WHEN m.status = 'claimed' AND m.claimed_by = $2::uuid
                           THEN m.claim_token ELSE gen_random_uuid() END,
        attempts    = CASE WHEN m.status = 'claimed' AND m.claimed_by = $2::uuid
                           THEN m.attempts    ELSE m.attempts + 1  END,
        claimed_at  = CASE WHEN m.status = 'claimed' AND m.claimed_by = $2::uuid
                           THEN m.claimed_at  ELSE now()           END,
        lease_expires_at = now() + make_interval(secs => COALESCE($3::int, m.lease_seconds))
    WHERE m.id = $1
      AND m.not_before <= now()
      AND (
            (m.status = 'open'    AND m.attempts < m.max_attempts)
         OR (m.status = 'claimed' AND m.lease_expires_at <= now()
                                  AND m.attempts < m.max_attempts)
         -- Retry-safe: re-claiming what you already hold returns the same token and
         -- does not burn an attempt, so a lost HTTP response is recoverable.
         OR (m.status = 'claimed' AND m.claimed_by = $2::uuid)
      )
    RETURNING m.*
)
SELECT {_CLAIMED}
FROM claimed AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
"""

CLAIM_NEXT = f"""
WITH claimed AS (
    UPDATE missions AS m
    SET status      = 'claimed',
        claimed_by  = $1::uuid,
        claim_token = gen_random_uuid(),
        attempts    = m.attempts + 1,
        claimed_at  = now(),
        lease_expires_at = now() + make_interval(secs => COALESCE($3::int, m.lease_seconds))
    WHERE m.id = (
        SELECT q.id
        FROM missions AS q
        WHERE q.required_art = ANY($2::text[])
          AND {_CLAIMABLE.format(a="q")}
        ORDER BY q.priority DESC, q.created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING m.*
)
SELECT {_CLAIMED}
FROM claimed AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
"""

# Deliberately does not require an unexpired lease: if nobody re-claimed the mission,
# a slow disciple recovering its lease is fine. If somebody did, claim_token no longer
# matches and this matches zero rows.
HEARTBEAT_MISSION = f"""
WITH beat AS (
    UPDATE missions AS m
    SET lease_expires_at = now() + make_interval(secs => COALESCE($4::int, m.lease_seconds))
    WHERE m.id = $1
      AND m.claimed_by = $2::uuid
      AND m.claim_token = $3::uuid
      AND m.status = 'claimed'
    RETURNING m.*
)
SELECT {_WRITTEN}
FROM beat AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
"""

# claim_token is NOT cleared here. Keeping it lets a retried complete() be recognised
# as a replay rather than a conflict; it never reaches the wire.
COMPLETE_MISSION = f"""
WITH done AS (
    UPDATE missions AS m
    SET status = 'completed', result = $3, error = NULL, finished_at = now()
    WHERE m.id = $1
      AND m.claimed_by = $2::uuid
      AND m.claim_token = $4::uuid
      AND m.status = 'claimed'
    RETURNING m.*
)
SELECT {_WRITTEN}
FROM done AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
"""

# $5 retryable, $6 retry_after_seconds. A retryable failure with attempts left goes back
# on the board with the holder columns cleared; anything else is terminal.
FAIL_MISSION = f"""
WITH failed AS (
    UPDATE missions AS m
    SET status = CASE WHEN $5::boolean AND m.attempts < m.max_attempts
                      THEN 'open' ELSE 'failed' END,
        error  = $3,
        not_before = CASE WHEN $5::boolean AND m.attempts < m.max_attempts
                          THEN now() + make_interval(secs => COALESCE($6::int, 60))
                          ELSE m.not_before END,
        claimed_by = CASE WHEN $5::boolean AND m.attempts < m.max_attempts
                          THEN NULL ELSE m.claimed_by END,
        claim_token = CASE WHEN $5::boolean AND m.attempts < m.max_attempts
                           THEN NULL ELSE m.claim_token END,
        claimed_at = CASE WHEN $5::boolean AND m.attempts < m.max_attempts
                          THEN NULL ELSE m.claimed_at END,
        lease_expires_at = CASE WHEN $5::boolean AND m.attempts < m.max_attempts
                                THEN NULL ELSE m.lease_expires_at END,
        finished_at = CASE WHEN $5::boolean AND m.attempts < m.max_attempts
                           THEN NULL ELSE now() END
    WHERE m.id = $1
      AND m.claimed_by = $2::uuid
      AND m.claim_token = $4::uuid
      AND m.status = 'claimed'
    RETURNING m.*
)
SELECT {_WRITTEN}
FROM failed AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
"""

CANCEL_MISSION = f"""
WITH cancelled AS (
    UPDATE missions AS m
    SET status = 'cancelled', finished_at = now(),
        claim_token = NULL, lease_expires_at = NULL
    WHERE m.id = $1 AND m.status IN ('open', 'claimed')
    RETURNING m.*
)
SELECT {_WRITTEN}
FROM cancelled AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
"""

# A mission that has burned every attempt stops matching the claimable predicate while
# still sitting in 'claimed'. This is the only reason a sweep exists: without it the
# board would show permanent zombies.
SWEEP_EXHAUSTED = """
UPDATE missions
SET status = 'failed',
    finished_at = now(),
    error = CASE WHEN error IS NULL OR error = ''
                 THEN 'lease expired; attempts exhausted'
                 ELSE error || ' | lease expired; attempts exhausted' END
WHERE status = 'claimed'
  AND lease_expires_at <= now()
  AND attempts >= max_attempts
RETURNING id
"""


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

STATS_MISSIONS = "SELECT status, count(*) AS n FROM missions GROUP BY status"

STATS_BY_ART = """
SELECT required_art, status, count(*) AS n
FROM missions
GROUP BY required_art, status
ORDER BY required_art
"""

STATS_DISCIPLES = """
SELECT count(*) AS total, count(*) FILTER (WHERE active) AS active FROM disciples
"""
