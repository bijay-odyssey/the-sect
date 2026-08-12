"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Depends, Request

from sect.core.settings import Settings


async def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
