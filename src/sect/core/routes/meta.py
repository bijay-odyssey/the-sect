"""Health, stats, and the manual sweep."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sect import __version__
from sect.core import sql
from sect.core.auth import AnyPrincipal, MasterPrincipal
from sect.core.deps import PoolDep
from sect.models import DiscipleCounts, Health, MissionCounts, SectStats, SweepResult

router = APIRouter(tags=["meta"])

#: Mounted at the root, outside /v1: it is infrastructure, not protocol.
health_router = APIRouter(tags=["meta"])
log = logging.getLogger(__name__)


@health_router.get("/health", response_model=Health)
async def health(request: Request) -> JSONResponse:
    """Unauthenticated liveness check. 503 when the database is unreachable."""
    db: str = "ok"
    try:
        await request.app.state.pool.fetchval("SELECT 1")
    except Exception as exc:
        log.warning("health check: database probe failed: %r", exc)
        db = "unreachable"

    payload = Health(
        status="ok" if db == "ok" else "degraded",
        db="ok" if db == "ok" else "unreachable",
        version=__version__,
        time=datetime.now(UTC),
    )
    return JSONResponse(payload.model_dump(mode="json"), status_code=200 if db == "ok" else 503)


@router.get("/stats", response_model=SectStats)
async def stats(principal: AnyPrincipal, pool: PoolDep) -> SectStats:
    """Counts for a status board. The data source for the dashboard that doesn't
    exist yet."""
    mission_rows = await pool.fetch(sql.STATS_MISSIONS)
    art_rows = await pool.fetch(sql.STATS_BY_ART)
    disciple_row = await pool.fetchrow(sql.STATS_DISCIPLES)

    totals = MissionCounts(**{row["status"]: row["n"] for row in mission_rows})

    per_art: dict[str, dict[str, int]] = {}
    for row in art_rows:
        per_art.setdefault(row["required_art"], {})[row["status"]] = row["n"]

    return SectStats(
        missions=totals,
        by_art={art: MissionCounts(**counts) for art, counts in per_art.items()},
        disciples=DiscipleCounts(
            total=disciple_row["total"] or 0,
            active=disciple_row["active"] or 0,
        ),
    )


@router.post("/admin/sweep", response_model=SweepResult)
async def sweep(principal: MasterPrincipal, pool: PoolDep) -> SweepResult:
    """Mark zombies failed on demand.

    The same sweep runs on every poll and claim-next, so this exists for operators who
    want it deterministic rather than because anything depends on it.
    """
    rows = await pool.fetch(sql.SWEEP_EXHAUSTED)
    return SweepResult(swept=len(rows))
