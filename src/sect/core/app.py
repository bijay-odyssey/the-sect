"""The FastAPI application.

Run it with::

    uvicorn sect.core.app:create_app --factory --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sect import __version__
from sect.core.db import create_pool, run_migrations
from sect.core.exceptions import SectHTTPError
from sect.core.logs import access_log_middleware, configure
from sect.core.routes import disciples, meta, missions, peaks
from sect.core.settings import Settings

log = logging.getLogger("sect.core")

_STATUS_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    500: "internal_error",
}

DESCRIPTION = """
A shared registry and mission board for a collection of small autonomous services.

Disciples register with an art, poll the Mission Hall for work matching it, claim a
mission atomically, execute it however they like, and report the result back.

Authenticate with `Authorization: Bearer <token>` -- either the master key or a
disciple token issued at registration.
"""


def create_app(
    settings: Settings | None = None,
    pool: asyncpg.Pool | None = None,
) -> FastAPI:
    """Build the app.

    ``pool`` is injectable so tests can supply their own connection pool and skip the
    lifespan's ownership of it.
    """
    settings = settings or Settings.from_env()
    configure(settings.log_level, json_lines=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_pool = app.state.pool is None
        if owns_pool:
            app.state.pool = await create_pool(app.state.settings)
        if app.state.settings.auto_migrate:
            applied = await run_migrations(app.state.pool)
            if applied:
                log.info("applied migrations: %s", ", ".join(applied))
        try:
            yield
        finally:
            if owns_pool and app.state.pool is not None:
                await app.state.pool.close()

    app = FastAPI(
        title="The Sect",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.pool = pool
    app.middleware("http")(access_log_middleware)

    # --- one error envelope, everywhere ------------------------------------ #

    @app.exception_handler(SectHTTPError)
    async def _sect_error(request: Request, exc: SectHTTPError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "The request did not match the expected shape.",
                    "detail": {"errors": jsonable_encoder(exc.errors())},
                }
            },
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": _STATUS_CODES.get(exc.status_code, "http_error"),
                    "message": str(exc.detail),
                }
            },
            status_code=exc.status_code,
        )

    # --- routes ------------------------------------------------------------- #

    v1 = APIRouter(prefix="/v1")
    v1.include_router(disciples.router)
    v1.include_router(missions.router)
    v1.include_router(peaks.router)
    v1.include_router(meta.router)

    app.include_router(v1)
    app.include_router(meta.health_router)

    return app
