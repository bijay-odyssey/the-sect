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
    """Columns for a wire-shaped mission, joined to ``disciples AS d`` for the holder
    name and ``peaks AS pk`` for the routing-hint name.

    ``alias`` is always a module-internal constant, never user input. Every query using
    this projection must also ``LEFT JOIN peaks AS pk ON pk.id = <alias>.peak_id``.
    """
    a = alias
    return f"""
        {a}.id, {a}.title, {a}.description, {a}.required_art, {a}.payload,
        {a}.priority, {a}.status, {a}.attempts, {a}.max_attempts, {a}.lease_seconds,
        {a}.not_before, d.name AS claimed_by, {a}.claimed_at, {a}.lease_expires_at,
        {a}.result, {a}.error, {a}.idempotency_key, {a}.posted_by, pk.name AS peak,
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
    d.agent_version, d.active, d.last_seen_at, d.created_at, pk.name AS peak,
    d.contribution_points, d.completed_missions, d.failed_missions,
    d.success_rate, d.reputation,
    COALESCE(h.claimed, 0) AS stat_claimed,
    d.completed_missions   AS stat_completed,
    d.failed_missions      AS stat_failed
"""

# Lifetime completed/failed counts are stored columns on `disciples`, maintained by
# COMPLETE_MISSION / FAIL_MISSION. Only `claimed` -- how many missions the disciple
# holds right now -- is transient and still counted live.
_DISCIPLE_HOLDINGS_JOIN = """
    LEFT JOIN LATERAL (
        SELECT count(*) AS claimed
        FROM missions AS m
        WHERE m.claimed_by = d.id AND m.status = 'claimed'
    ) AS h ON true
    LEFT JOIN peaks AS pk ON pk.id = d.peak_id
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
SELECT id, name, arts, realm, active, peak_id
FROM disciples
WHERE token_hash = $1
"""

INSERT_DISCIPLE = """
INSERT INTO disciples (name, display_name, arts, repo_url, description, token_hash, peak_id)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (name) DO NOTHING
RETURNING id
"""

SELECT_DISCIPLE = f"""
SELECT {_DISCIPLE}
FROM disciples AS d
{_DISCIPLE_HOLDINGS_JOIN}
WHERE d.name = $1
"""

LIST_DISCIPLES = f"""
SELECT {_DISCIPLE}
FROM disciples AS d
{_DISCIPLE_HOLDINGS_JOIN}
WHERE ($1::text IS NULL OR $1 = ANY(d.arts))
  AND ($2::text IS NULL OR d.realm = $2)
  AND ($3::boolean IS NULL OR d.active = $3)
ORDER BY d.name
"""

ROTATE_TOKEN = """
UPDATE disciples SET token_hash = $2 WHERE name = $1 RETURNING id
"""

#: Columns a disciple may change about itself. Realm is absent by design; ``peak_id`` is
#: present because a disciple may join or leave a peak on its own.
SELF_UPDATABLE: tuple[str, ...] = (
    "display_name",
    "arts",
    "repo_url",
    "description",
    "agent_version",
    "peak_id",
)

#: Columns the master may change. This is where ascension happens.
MASTER_UPDATABLE: tuple[str, ...] = (
    "display_name",
    "arts",
    "repo_url",
    "description",
    "realm",
    "active",
    "peak_id",
)

#: Columns the master may change on a peak.
PEAK_UPDATABLE: tuple[str, ...] = (
    "display_name",
    "description",
    "arts",
    "status",
)


def build_update(
    table: str,
    fields: dict[str, Any],
    allowed: tuple[str, ...],
    *,
    key_column: str = "id",
    touch_last_seen: bool = False,
) -> tuple[str, list[Any]]:
    """Build a partial UPDATE from explicitly-supplied fields.

    Callers pass ``model_dump(exclude_unset=True)``, so "not mentioned" and "set to
    null" stay distinguishable and a disciple can refresh only its ``agent_version``
    without wiping its description.

    ``table``, ``allowed`` and ``key_column`` are all module constants -- never request
    input; only values are parameterized. The last element of the returned args list is
    a ``None`` placeholder for the ``WHERE`` value, which the caller fills in.
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
    sql = (
        f"UPDATE {table} SET {', '.join(assignments)} "
        f"WHERE {key_column} = ${where_index} RETURNING {key_column}"
    )
    return sql, args


# --------------------------------------------------------------------------- #
# Missions: create and read
# --------------------------------------------------------------------------- #

INSERT_MISSION = f"""
WITH ins AS (
    INSERT INTO missions (
        title, description, required_art, payload, priority,
        lease_seconds, max_attempts, not_before, idempotency_key, posted_by, peak_id
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8::timestamptz, now()), $9, $10, $11)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING missions.*
)
SELECT {_WRITTEN}
FROM ins AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
LEFT JOIN peaks AS pk ON pk.id = c.peak_id
"""

SELECT_MISSION = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
LEFT JOIN peaks AS pk ON pk.id = m.peak_id
WHERE m.id = $1
"""

SELECT_MISSION_BY_IDEMPOTENCY_KEY = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
LEFT JOIN peaks AS pk ON pk.id = m.peak_id
WHERE m.idempotency_key = $1
"""

# $3 is the caller's peak, or NULL. A mission whose peak_id matches sorts first for that
# peak's disciples -- but peak_id never restricts which rows are returned (a peak is not
# a wall), so this stays a pure ORDER BY term.
LIST_OPEN_MISSIONS = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
LEFT JOIN peaks AS pk ON pk.id = m.peak_id
WHERE m.required_art = ANY($1::text[])
  AND {_CLAIMABLE.format(a="m")}
ORDER BY COALESCE($3::uuid IS NOT NULL AND m.peak_id = $3::uuid, false) DESC,
         m.priority DESC, m.created_at ASC
LIMIT $2
"""

# $6/$7 carry the read scope: NULL for the master (sees everything), otherwise the
# calling disciple's id and name, so it sees only what it holds or posted.
LIST_MISSIONS = f"""
SELECT {_MISSION}
FROM missions AS m
LEFT JOIN disciples AS d ON d.id = m.claimed_by
LEFT JOIN peaks AS pk ON pk.id = m.peak_id
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
LEFT JOIN peaks AS pk ON pk.id = c.peak_id
"""

# $4 is the claiming disciple's peak, or NULL. It reorders candidates so a disciple
# takes its own peak's work first; it is NOT part of the claimable predicate, so a
# disciple can still be dealt any mission matching its arts (a peak is not a wall).
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
        ORDER BY COALESCE($4::uuid IS NOT NULL AND q.peak_id = $4::uuid, false) DESC,
                 q.priority DESC, q.created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING m.*
)
SELECT {_CLAIMED}
FROM claimed AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
LEFT JOIN peaks AS pk ON pk.id = c.peak_id
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
LEFT JOIN peaks AS pk ON pk.id = c.peak_id
"""

# claim_token is NOT cleared here. Keeping it lets a retried complete() be recognised
# as a replay rather than a conflict; it never reaches the wire.
#
# The `credit` CTE is a data-modifying WITH: PostgreSQL runs it exactly once and to
# completion whether or not the primary query reads its output, so the contribution
# ledger moves in the same statement -- the same implicit transaction -- as the mission
# status. It fires only when `done` matched a row, so a replayed complete() (which
# matches nothing) never double-counts. Nothing was added to the missions UPDATE's
# WHERE clause: the guard is still one conditional UPDATE.
COMPLETE_MISSION = f"""
WITH done AS (
    UPDATE missions AS m
    SET status = 'completed', result = $3, error = NULL, finished_at = now()
    WHERE m.id = $1
      AND m.claimed_by = $2::uuid
      AND m.claim_token = $4::uuid
      AND m.status = 'claimed'
    RETURNING m.*
),
credit AS (
    UPDATE disciples AS dd
    SET completed_missions  = dd.completed_missions + 1,
        contribution_points = dd.contribution_points + 1,
        success_rate        = (dd.completed_missions + 1)::double precision
                              / (dd.completed_missions + 1 + dd.failed_missions),
        reputation          = floor(
                                  (dd.contribution_points + 1)
                                  * ((dd.completed_missions + 1)::double precision
                                     / (dd.completed_missions + 1 + dd.failed_missions))
                              )::int
    FROM done
    WHERE dd.id = done.claimed_by
    RETURNING dd.id
)
SELECT {_WRITTEN}
FROM done AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
LEFT JOIN peaks AS pk ON pk.id = c.peak_id
"""

# $5 retryable, $6 retry_after_seconds, $7 contribution-point penalty for a terminal
# failure (0 by default). A retryable failure with attempts left goes back on the board
# with the holder columns cleared and is attributed to nobody; anything else is terminal
# and moves the holder's ledger via the `debit` CTE (same once-and-to-completion rule as
# `credit` in COMPLETE_MISSION).
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
    RETURNING m.*,
              NOT ($5::boolean AND m.attempts < m.max_attempts) AS terminal,
              m.claimed_by AS holder_id
),
debit AS (
    UPDATE disciples AS dd
    SET failed_missions     = dd.failed_missions + 1,
        contribution_points = GREATEST(0, dd.contribution_points - COALESCE($7::int, 0)),
        success_rate        = dd.completed_missions::double precision
                              / NULLIF(dd.completed_missions + dd.failed_missions + 1, 0),
        reputation          = floor(
                                  GREATEST(0, dd.contribution_points - COALESCE($7::int, 0))
                                  * (dd.completed_missions::double precision
                                     / NULLIF(dd.completed_missions + dd.failed_missions + 1, 0))
                              )::int
    FROM failed
    WHERE failed.terminal AND dd.id = failed.holder_id
    RETURNING dd.id
)
SELECT {_WRITTEN}
FROM failed AS c
LEFT JOIN disciples AS d ON d.id = c.claimed_by
LEFT JOIN peaks AS pk ON pk.id = c.peak_id
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
LEFT JOIN peaks AS pk ON pk.id = c.peak_id
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


# --------------------------------------------------------------------------- #
# Peaks
# --------------------------------------------------------------------------- #

_PEAK = """
    p.name, p.display_name, p.description, p.arts,
    p.status, p.last_seen_at, p.created_at, p.updated_at,
    COALESCE(s.disciples, 0)          AS stat_disciples,
    COALESCE(s.completed_missions, 0) AS stat_completed_missions
"""

# Display roll-up only. Contribution points live on the disciple and follow it across
# peaks, so this never feeds an authorization or routing decision.
_PEAK_STATS_JOIN = """
    LEFT JOIN LATERAL (
        SELECT count(*)                              AS disciples,
               COALESCE(sum(d.completed_missions), 0) AS completed_missions
        FROM disciples AS d
        WHERE d.peak_id = p.id
    ) AS s ON true
"""

CREATE_PEAK = """
INSERT INTO peaks (name, display_name, description, arts)
VALUES ($1, $2, $3, $4)
ON CONFLICT (name) DO NOTHING
RETURNING id
"""

SELECT_PEAK = f"""
SELECT {_PEAK}
FROM peaks AS p
{_PEAK_STATS_JOIN}
WHERE p.name = $1
"""

LIST_PEAKS = f"""
SELECT {_PEAK}
FROM peaks AS p
{_PEAK_STATS_JOIN}
WHERE ($1::text IS NULL OR $1 = ANY(p.arts))
  AND ($2::text IS NULL OR p.status = $2)
ORDER BY p.name
"""

DEACTIVATE_PEAK = """
UPDATE peaks SET status = 'inactive' WHERE name = $1 RETURNING id
"""

PEAK_HEARTBEAT = """
UPDATE peaks SET last_seen_at = now() WHERE name = $1 RETURNING last_seen_at
"""
