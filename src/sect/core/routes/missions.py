"""The Mission Hall.

Every transition below is one conditional UPDATE (see :mod:`sect.core.sql`). When a
statement matches zero rows the handler runs ``INSPECT_MISSION`` purely to *explain*
the 409 -- never to decide whether the write should have happened.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Query, Response

from sect.core import sql
from sect.core.auth import AnyPrincipal, DisciplePrincipal, MasterPrincipal, Principal
from sect.core.deps import PoolDep, SettingsDep
from sect.core.exceptions import SectHTTPError
from sect.core.rows import mission_from_row
from sect.core.settings import Settings
from sect.models import (
    ClaimNextRequest,
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    FailRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    Mission,
    MissionCreate,
    MissionList,
    MissionStatus,
    OpenMissionList,
)

router = APIRouter(prefix="/missions", tags=["missions"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_lease(requested: int | None, settings: Settings) -> int | None:
    """None means "use the mission's own lease_seconds", which the SQL COALESCEs."""
    if requested is None:
        return None
    if requested > settings.max_lease_seconds:
        raise SectHTTPError(
            400,
            "lease_too_long",
            f"lease_seconds may not exceed {settings.max_lease_seconds}.",
            {"max_lease_seconds": settings.max_lease_seconds},
        )
    return requested


def _authorized_arts(principal: Principal, requested: list[str] | None) -> list[str]:
    """Which arts this caller may look at.

    A disciple polls its own arts and only its own: the board is shared, but a worker
    has no business browsing work it never declared it could do.
    """
    if principal.is_master:
        if not requested:
            raise SectHTTPError(
                400,
                "art_required",
                "The master has no registered arts to default to; pass at least one ?art=.",
            )
        return requested
    arts = requested or list(principal.arts)
    unknown = sorted(set(arts) - set(principal.arts))
    if unknown:
        raise SectHTTPError(
            403,
            "forbidden_art",
            f"Disciple '{principal.name}' has not registered the art(s): {', '.join(unknown)}.",
            {"arts": unknown},
        )
    return arts


def _encode_cursor(created_at: datetime, mission_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{mission_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        timestamp, _, mission_id = raw.partition("|")
        return datetime.fromisoformat(timestamp), UUID(mission_id)
    except (ValueError, binascii.Error) as exc:
        raise SectHTTPError(400, "bad_cursor", "Cursor is not one this server issued.") from exc


async def _explain_claim_failure(pool: asyncpg.Pool, mission_id: UUID) -> SectHTTPError:
    """Turn a zero-row claim into a 409 that says something useful."""
    row = await pool.fetchrow(sql.INSPECT_MISSION, mission_id, None)
    if row is None:
        return SectHTTPError(404, "mission_not_found", f"No mission with id {mission_id}.")

    status = row["status"]
    detail: dict[str, Any] = {
        "status": status,
        "claimed_by": row["claimed_by"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
    }

    if status in ("completed", "failed", "cancelled"):
        message = f"Mission is already {status}."
    elif status == "claimed":
        message = (
            f"Mission is held by disciple '{row['claimed_by']}' "
            f"until {row['lease_expires_at'].isoformat()}."
        )
    elif row["not_before"] > datetime.now(UTC):
        message = f"Mission is not scheduled to run until {row['not_before'].isoformat()}."
        detail["not_before"] = row["not_before"].isoformat()
    elif row["attempts"] >= row["max_attempts"]:
        message = f"Mission has used all {row['max_attempts']} of its attempts."
    else:  # pragma: no cover - defensive
        message = "Mission is not claimable."

    return SectHTTPError(409, "mission_not_claimable", message, detail)


def _holder_conflict(row: Mapping[str, Any]) -> SectHTTPError:
    """Explain why a holder-guarded write matched nothing."""
    status = row["status"]
    if status == "cancelled":
        reason, message = "cancelled", "Mission was cancelled."
    elif status == "completed":
        reason, message = "already_completed", "Mission was already completed by another claim."
    elif status == "failed":
        reason, message = "already_failed", "Mission has already failed terminally."
    elif status == "claimed":
        reason, message = (
            "reclaimed",
            f"Your lease expired and disciple '{row['claimed_by']}' now holds this mission.",
        )
    else:
        reason, message = (
            "lease_expired",
            "Your lease expired and the mission went back on the board.",
        )
    return SectHTTPError(
        409,
        "not_mission_holder",
        message,
        {"status": status, "reason": reason, "claimed_by": row["claimed_by"]},
    )


async def _load_holder_state(
    pool: asyncpg.Pool, mission_id: UUID, claim_token: UUID
) -> Mapping[str, Any]:
    row = await pool.fetchrow(sql.INSPECT_MISSION, mission_id, claim_token)
    if row is None:
        raise SectHTTPError(404, "mission_not_found", f"No mission with id {mission_id}.")
    return row


# --------------------------------------------------------------------------- #
# Posting and polling
# --------------------------------------------------------------------------- #


@router.post("", response_model=Mission, status_code=201)
async def post_mission(
    body: MissionCreate,
    principal: AnyPrincipal,
    pool: PoolDep,
    settings: SettingsDep,
    response: Response,
) -> Mission:
    """Post a mission. Master or disciple; ``posted_by`` records which."""
    lease = body.lease_seconds or settings.default_lease_seconds
    _resolve_lease(lease, settings)
    max_attempts = body.max_attempts or settings.default_max_attempts

    row = await pool.fetchrow(
        sql.INSERT_MISSION,
        body.title,
        body.description,
        body.required_art,
        body.payload,
        body.priority,
        lease,
        max_attempts,
        body.not_before,
        body.idempotency_key,
        principal.name,
    )

    if row is None:
        # ON CONFLICT DO NOTHING fired: this idempotency_key already exists. A replay,
        # not an error -- hand back what was posted the first time.
        row = await pool.fetchrow(sql.SELECT_MISSION_BY_IDEMPOTENCY_KEY, body.idempotency_key)
        if row is None:  # pragma: no cover - only if the key vanished mid-flight
            raise SectHTTPError(409, "mission_conflict", "Mission could not be posted.")
        response.status_code = 200

    return mission_from_row(row)


@router.get("/open", response_model=OpenMissionList)
async def list_open_missions(
    principal: AnyPrincipal,
    pool: PoolDep,
    settings: SettingsDep,
    art: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 20,
) -> OpenMissionList:
    """Claimable missions for these arts, best first.

    Deliberately readable by any disciple: you cannot decide whether to claim a mission
    whose brief you are not allowed to read. Only unclaimed work appears here, never a
    result.
    """
    await pool.execute(sql.SWEEP_EXHAUSTED)
    arts = _authorized_arts(principal, art)
    rows = await pool.fetch(sql.LIST_OPEN_MISSIONS, arts, min(limit, settings.max_poll_limit))
    missions = [mission_from_row(row) for row in rows]
    return OpenMissionList(missions=missions, count=len(missions))


@router.get("", response_model=MissionList)
async def browse_missions(
    principal: AnyPrincipal,
    pool: PoolDep,
    settings: SettingsDep,
    status: Annotated[MissionStatus | None, Query()] = None,
    art: Annotated[str | None, Query()] = None,
    claimed_by: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> MissionList:
    """Browse the board. A disciple sees only what it holds or posted."""
    claimed_by_id: UUID | None = None
    if claimed_by is not None:
        row = await pool.fetchrow("SELECT id FROM disciples WHERE name = $1", claimed_by)
        if row is None:
            raise SectHTTPError(404, "disciple_not_found", f"No disciple named '{claimed_by}'.")
        claimed_by_id = row["id"]

    after_time, after_id = (None, None)
    if cursor:
        after_time, after_id = _decode_cursor(cursor)

    limit = min(limit, settings.max_poll_limit)
    rows = await pool.fetch(
        sql.LIST_MISSIONS,
        status,
        art,
        claimed_by_id,
        after_time,
        after_id,
        None if principal.is_master else principal.disciple_id,
        None if principal.is_master else principal.name,
        limit + 1,  # one extra row tells us whether another page exists
    )

    has_more = len(rows) > limit
    rows = rows[:limit]
    missions = [mission_from_row(row) for row in rows]
    next_cursor = (
        _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
    )
    return MissionList(missions=missions, count=len(missions), next_cursor=next_cursor)


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #


@router.post("/claim-next", response_model=ClaimResponse)
async def claim_next_mission(
    principal: DisciplePrincipal,
    pool: PoolDep,
    settings: SettingsDep,
    response: Response,
    body: ClaimNextRequest | None = None,
) -> ClaimResponse | Response:
    """Atomically take the best matching mission.

    ``FOR UPDATE SKIP LOCKED`` means concurrent disciples skip each other's in-flight
    rows instead of queueing, so twenty workers waking on the same cron minute get
    twenty different missions with no retries and no collisions.
    """
    await pool.execute(sql.SWEEP_EXHAUSTED)
    body = body or ClaimNextRequest()
    arts = _authorized_arts(principal, body.arts)
    lease = _resolve_lease(body.lease_seconds, settings)

    row = await pool.fetchrow(sql.CLAIM_NEXT, principal.disciple_id, arts, lease)
    if row is None:
        return Response(status_code=204)
    return ClaimResponse(mission=mission_from_row(row), claim_token=row["claim_token"])


@router.post("/{mission_id}/claim", response_model=ClaimResponse)
async def claim_mission(
    mission_id: UUID,
    principal: DisciplePrincipal,
    pool: PoolDep,
    settings: SettingsDep,
    body: ClaimRequest | None = None,
) -> ClaimResponse:
    """Atomically claim one specific mission.

    Exactly one concurrent caller gets a row back; everyone else gets 409. Re-claiming
    a mission you already hold is idempotent -- same token, no extra attempt -- so a
    lost response is recoverable.
    """
    body = body or ClaimRequest()
    lease = _resolve_lease(body.lease_seconds, settings)

    row = await pool.fetchrow(sql.CLAIM_MISSION, mission_id, principal.disciple_id, lease)
    if row is None:
        raise await _explain_claim_failure(pool, mission_id)
    return ClaimResponse(mission=mission_from_row(row), claim_token=row["claim_token"])


# --------------------------------------------------------------------------- #
# Finishing
# --------------------------------------------------------------------------- #


@router.post("/{mission_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat_mission(
    mission_id: UUID,
    body: HeartbeatRequest,
    principal: DisciplePrincipal,
    pool: PoolDep,
    settings: SettingsDep,
) -> HeartbeatResponse:
    """Extend the lease on a mission you hold."""
    extend = _resolve_lease(body.extend_seconds, settings)
    row = await pool.fetchrow(
        sql.HEARTBEAT_MISSION, mission_id, principal.disciple_id, body.claim_token, extend
    )
    if row is None:
        raise _holder_conflict(await _load_holder_state(pool, mission_id, body.claim_token))
    return HeartbeatResponse(lease_expires_at=row["lease_expires_at"])


@router.post("/{mission_id}/complete", response_model=Mission)
async def complete_mission(
    mission_id: UUID,
    body: CompleteRequest,
    principal: DisciplePrincipal,
    pool: PoolDep,
) -> Mission:
    """Report success.

    Guarded by ``claim_token``, so a disciple whose lease expired mid-run cannot
    overwrite the result of whoever redid the work. An exact replay by the true holder
    returns 200 rather than a conflict, which is what makes ``complete`` safe to retry
    over a flaky connection.
    """
    row = await pool.fetchrow(
        sql.COMPLETE_MISSION, mission_id, principal.disciple_id, body.result, body.claim_token
    )
    if row is not None:
        return mission_from_row(row)

    state = await _load_holder_state(pool, mission_id, body.claim_token)
    is_replay = (
        state["status"] == "completed"
        and state["token_matches"]
        and state["claimed_by_id"] == principal.disciple_id
    )
    if is_replay:
        existing = await pool.fetchrow(sql.SELECT_MISSION, mission_id)
        return mission_from_row(existing)
    raise _holder_conflict(state)


@router.post("/{mission_id}/fail", response_model=Mission)
async def fail_mission(
    mission_id: UUID,
    body: FailRequest,
    principal: DisciplePrincipal,
    pool: PoolDep,
) -> Mission:
    """Report failure. Retryable failures go back on the board after a backoff."""
    row = await pool.fetchrow(
        sql.FAIL_MISSION,
        mission_id,
        principal.disciple_id,
        body.error,
        body.claim_token,
        body.retryable,
        body.retry_after_seconds,
    )
    if row is None:
        raise _holder_conflict(await _load_holder_state(pool, mission_id, body.claim_token))
    return mission_from_row(row)


@router.post("/{mission_id}/cancel", response_model=Mission)
async def cancel_mission(
    mission_id: UUID,
    principal: MasterPrincipal,
    pool: PoolDep,
) -> Mission:
    """Pull a mission off the board. Master only."""
    row = await pool.fetchrow(sql.CANCEL_MISSION, mission_id)
    if row is not None:
        return mission_from_row(row)

    existing = await pool.fetchrow(sql.SELECT_MISSION, mission_id)
    if existing is None:
        raise SectHTTPError(404, "mission_not_found", f"No mission with id {mission_id}.")
    raise SectHTTPError(
        409,
        "mission_already_finished",
        f"Mission is already {existing['status']} and cannot be cancelled.",
        {"status": existing["status"]},
    )


# --------------------------------------------------------------------------- #
# Reading one
# --------------------------------------------------------------------------- #


@router.get("/{mission_id}", response_model=Mission)
async def get_mission(
    mission_id: UUID,
    principal: AnyPrincipal,
    pool: PoolDep,
) -> Mission:
    """Read a mission and its result. Master, current holder, or poster."""
    row = await pool.fetchrow(sql.SELECT_MISSION, mission_id)
    if row is None:
        raise SectHTTPError(404, "mission_not_found", f"No mission with id {mission_id}.")

    if not principal.is_master:
        is_holder = row["claimed_by_id"] == principal.disciple_id
        is_poster = row["posted_by"] == principal.name
        if not (is_holder or is_poster):
            raise SectHTTPError(
                403,
                "mission_forbidden",
                "A mission is readable by the master, the disciple holding it, "
                "and the account that posted it.",
            )
    return mission_from_row(row)
