"""The Sect Registry: peaks -- named specialty groups a disciple may belong to.

A peak is metadata plus a routing preference. It is *not* a wall: a mission tagged with
a peak is still claimable by any disciple whose arts match (see ``sql.CLAIM_NEXT`` and
``sql.LIST_OPEN_MISSIONS`` -- ``peak_id`` appears only in the ``ORDER BY``). All peak
mutation is master-only in v0.2.0; a peak has no token of its own.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from sect.core import sql
from sect.core.auth import AnyPrincipal, MasterPrincipal
from sect.core.deps import PoolDep
from sect.core.exceptions import SectHTTPError
from sect.core.rows import peak_from_row
from sect.models import (
    Peak,
    PeakCreate,
    PeakHeartbeatResponse,
    PeakList,
    PeakPatch,
    PeakStatus,
)

router = APIRouter(prefix="/peaks", tags=["peaks"])


@router.post("", response_model=Peak, status_code=201)
async def create_peak(body: PeakCreate, principal: MasterPrincipal, pool: PoolDep) -> Peak:
    """Register a peak. Master only."""
    created = await pool.fetchrow(
        sql.CREATE_PEAK,
        body.name,
        body.display_name,
        body.description,
        body.arts,
    )
    if created is None:
        raise SectHTTPError(
            409, "peak_exists", f"A peak named '{body.name}' already exists.", {"name": body.name}
        )
    return peak_from_row(await pool.fetchrow(sql.SELECT_PEAK, body.name))


@router.get("", response_model=PeakList)
async def list_peaks(
    principal: AnyPrincipal,
    pool: PoolDep,
    art: Annotated[str | None, Query()] = None,
    status: Annotated[PeakStatus | None, Query()] = None,
) -> PeakList:
    """List peaks. Defaults to the active roster; pass ``?status=`` for another set."""
    rows = await pool.fetch(sql.LIST_PEAKS, art, status or "active")
    peaks = [peak_from_row(row) for row in rows]
    return PeakList(peaks=peaks, count=len(peaks))


@router.get("/{name}", response_model=Peak)
async def get_peak(name: str, principal: AnyPrincipal, pool: PoolDep) -> Peak:
    row = await pool.fetchrow(sql.SELECT_PEAK, name)
    if row is None:
        raise SectHTTPError(404, "peak_not_found", f"No peak named '{name}'.")
    return peak_from_row(row)


@router.patch("/{name}", response_model=Peak)
async def patch_peak(
    name: str,
    body: PeakPatch,
    principal: MasterPrincipal,
    pool: PoolDep,
) -> Peak:
    """Edit a peak's metadata, arts, or status. Master only."""
    existing = await pool.fetchrow("SELECT name FROM peaks WHERE name = $1", name)
    if existing is None:
        raise SectHTTPError(404, "peak_not_found", f"No peak named '{name}'.")

    fields = body.model_dump(exclude_unset=True)
    statement, args = sql.build_update("peaks", fields, sql.PEAK_UPDATABLE, key_column="name")
    if statement:
        args[-1] = name
        await pool.execute(statement, *args)
    return peak_from_row(await pool.fetchrow(sql.SELECT_PEAK, name))


@router.delete("/{name}", response_model=Peak)
async def deactivate_peak(name: str, principal: MasterPrincipal, pool: PoolDep) -> Peak:
    """Soft delete: set ``status = 'inactive'``. Disciples keep their affiliation and
    their contribution ledger; the peak simply drops out of the default roster."""
    done = await pool.fetchrow(sql.DEACTIVATE_PEAK, name)
    if done is None:
        raise SectHTTPError(404, "peak_not_found", f"No peak named '{name}'.")
    return peak_from_row(await pool.fetchrow(sql.SELECT_PEAK, name))


@router.post("/{name}/heartbeat", response_model=PeakHeartbeatResponse)
async def peak_heartbeat(
    name: str,
    principal: MasterPrincipal,
    pool: PoolDep,
) -> PeakHeartbeatResponse:
    """Record that a peak is alive. Master only in v0.2.0 (peaks have no token yet);
    it exists so an operator watching the registry can tell a dormant peak from a dead
    one."""
    row = await pool.fetchrow(sql.PEAK_HEARTBEAT, name)
    if row is None:
        raise SectHTTPError(404, "peak_not_found", f"No peak named '{name}'.")
    return PeakHeartbeatResponse(last_seen_at=row["last_seen_at"])
