"""Test fixtures.

Everything runs against a real PostgreSQL. There is no in-memory substitute for what
these tests check: row locks, ``EvalPlanQual`` re-evaluation under READ COMMITTED, and
``FOR UPDATE SKIP LOCKED`` are database behaviours, not application behaviours.

    docker run -d --name sect-pg -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_DB=sect_test -p 5432:5432 postgres:16-alpine
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import replace
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import uvicorn

from sect.core.app import create_app
from sect.core.db import create_pool, run_migrations
from sect.core.settings import Settings

# A developer's .env legitimately points at a real database. The suite TRUNCATEs on
# every test, so it must never read one. Set before anything calls ensure_loaded().
os.environ.setdefault("SECT_SKIP_DOTENV", "1")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/sect_test",
)

# Long enough to satisfy the production length check.
MASTER_KEY = "test-master-key-000000000000000000"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        master_key=MASTER_KEY,
        db_pool_min=2,
        # The concurrency tests fire twenty simultaneous requests; the pool must not
        # be the thing that serializes them, or they would prove nothing.
        db_pool_max=25,
        auto_migrate=False,
        # Plain-text logs keep pytest -q output readable.
        log_json=False,
    )


@pytest.fixture(scope="session", autouse=True)
def _migrated(settings: Settings) -> None:
    """Apply migrations once for the whole session, on their own event loop."""

    async def run() -> None:
        pool = await create_pool(settings)
        try:
            await run_migrations(pool)
        finally:
            await pool.close()

    asyncio.run(run())


@pytest_asyncio.fixture
async def pool(settings: Settings) -> AsyncIterator[asyncpg.Pool]:
    pool = await create_pool(settings)
    try:
        await pool.execute("TRUNCATE missions, disciples, peaks RESTART IDENTITY CASCADE")
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def client(settings: Settings, pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=settings, pool=pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sect.test") as client:
        yield client


class Sect:
    """Small facade so tests read like the story they are telling."""

    def __init__(self, client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
        self.client = client
        self.pool = pool
        self.master_headers = {"Authorization": f"Bearer {MASTER_KEY}"}

    @staticmethod
    def headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def create_disciple(
        self, name: str, arts: Iterable[str] = ("test",), *, peak: str | None = None
    ) -> str:
        """Register a disciple and return its token."""
        body: dict[str, Any] = {"name": name, "arts": list(arts)}
        if peak is not None:
            body["peak"] = peak
        response = await self.client.post("/v1/disciples", json=body, headers=self.master_headers)
        assert response.status_code == 201, response.text
        return response.json()["token"]

    async def create_peak(
        self, name: str, arts: Iterable[str] = ("test",), **overrides: Any
    ) -> dict[str, Any]:
        """Register a peak and return it."""
        body: dict[str, Any] = {
            "name": name,
            "display_name": overrides.pop("display_name", name.replace("-", " ").title()),
            "arts": list(arts),
            **overrides,
        }
        response = await self.client.post("/v1/peaks", json=body, headers=self.master_headers)
        assert response.status_code == 201, response.text
        return response.json()

    async def disciple(self, name: str) -> dict[str, Any]:
        response = await self.client.get(f"/v1/disciples/{name}", headers=self.master_headers)
        assert response.status_code == 200, response.text
        return response.json()

    async def disciple_id(self, name: str) -> Any:
        return await self.pool.fetchval("SELECT id FROM disciples WHERE name = $1", name)

    async def post_mission(self, art: str = "test", **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"title": "a mission", "required_art": art, **overrides}
        response = await self.client.post("/v1/missions", json=body, headers=self.master_headers)
        assert response.status_code == 201, response.text
        return response.json()

    async def claim(self, mission_id: str, token: str, **body: Any) -> httpx.Response:
        return await self.client.post(
            f"/v1/missions/{mission_id}/claim", json=body, headers=self.headers(token)
        )

    async def expire_lease(self, mission_id: str) -> None:
        """Age a lease out without waiting for wall-clock time.

        Reaches past the API deliberately: the point is to simulate a disciple that
        died, which by definition never calls anything.
        """
        await self.pool.execute(
            "UPDATE missions SET lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1::uuid",
            mission_id,
        )

    async def mission_row(self, mission_id: str) -> asyncpg.Record:
        return await self.pool.fetchrow("SELECT * FROM missions WHERE id = $1::uuid", mission_id)


@pytest.fixture
def sect(client: httpx.AsyncClient, pool: asyncpg.Pool) -> Sect:
    return Sect(client, pool)


# --------------------------------------------------------------------------- #
# Synchronous fixtures, for exercising the SDK
# --------------------------------------------------------------------------- #


class Db:
    """Blocking database access for the synchronous SDK tests."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _run(self, method: str, query: str, *args: Any) -> Any:
        async def go() -> Any:
            connection = await asyncpg.connect(self._dsn)
            try:
                for typename in ("jsonb", "json"):
                    await connection.set_type_codec(
                        typename,
                        encoder=json.dumps,
                        decoder=json.loads,
                        schema="pg_catalog",
                    )
                return await getattr(connection, method)(query, *args)
            finally:
                await connection.close()

        return asyncio.run(go())

    def fetchrow(self, query: str, *args: Any) -> Any:
        return self._run("fetchrow", query, *args)

    def fetchval(self, query: str, *args: Any) -> Any:
        return self._run("fetchval", query, *args)

    def execute(self, query: str, *args: Any) -> Any:
        return self._run("execute", query, *args)

    def mission(self, mission_id: Any) -> Any:
        return self.fetchrow("SELECT * FROM missions WHERE id = $1::uuid", str(mission_id))


@pytest.fixture
def db(settings: Settings) -> Db:
    return Db(settings.database_url)


@pytest.fixture
def clean_db(db: Db) -> None:
    db.execute("TRUNCATE missions, disciples, peaks RESTART IDENTITY CASCADE")


@pytest.fixture(scope="session")
def live_server(settings: Settings) -> Iterator[str]:
    """A real uvicorn on a real port.

    The SDK is synchronous by design -- a cron job is a sync script -- so it cannot be
    pointed at an in-process ASGI transport from an async test. Running the server for
    real is also the more honest test: it exercises the actual HTTP stack, connection
    reuse and all, which is exactly what the retry policy interacts with.
    """
    # A pool of its own, built inside uvicorn's event loop; small so the test database
    # does not run out of connections alongside the async suite.
    server_settings = replace(settings, db_pool_min=1, db_pool_max=5)
    app = create_app(settings=server_settings, pool=None)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="sect-test-server")
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not come up")
        time.sleep(0.02)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
