"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import Depends, Request

from sect.core.exceptions import SectHTTPError
from sect.core.settings import Settings


async def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def resolve_peak_id(pool: asyncpg.Pool, name: str | None) -> UUID | None:
    """Translate a peak *name* to its id.

    ``None`` in, ``None`` out (no affiliation / no routing hint). A name that does not
    match a registered peak is a 404 -- peaks are addressed by name on the wire, like
    disciples, and their ids never leave the server.
    """
    if name is None:
        return None
    row = await pool.fetchrow("SELECT id FROM peaks WHERE name = $1", name)
    if row is None:
        raise SectHTTPError(404, "peak_not_found", f"No peak named '{name}'.")
    return row["id"]
