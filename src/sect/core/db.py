"""Connection pool and migration runner.

Migrations ship inside the package (``sect/core/migrations``) rather than at the repo
root, because they are applied on boot and therefore have to exist in the wheel and in
the container image.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import asyncpg

from sect.core.settings import Settings

log = logging.getLogger("sect.core.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

#: Arbitrary but stable. Two instances booting during a deploy must not race to migrate.
MIGRATION_LOCK_ID = 0x53454354  # "SECT"

CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Teach asyncpg to hand jsonb columns back as Python objects.

    Without this, ``payload`` and ``result`` arrive as raw strings and every route has
    to remember to decode them.
    """
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def create_pool(settings: Settings) -> asyncpg.Pool:
    kwargs: dict[str, object] = {}
    if settings.db_pgbouncer:
        # Transaction-mode pooling and prepared statements do not mix.
        kwargs["statement_cache_size"] = 0
    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        init=_init_connection,
        **kwargs,
    )
    if pool is None:  # pragma: no cover - asyncpg only returns None on misuse
        raise RuntimeError("asyncpg.create_pool returned None")
    return pool


def migration_files() -> list[tuple[str, str]]:
    """``[(version, sql), ...]`` in lexical order, which is also apply order."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


async def run_migrations(pool: asyncpg.Pool) -> list[str]:
    """Apply every pending migration. Returns the versions applied by this call.

    Safe to run concurrently from several instances: the advisory lock serializes them
    and the loser finds nothing left to do.
    """
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)
        try:
            await conn.execute(CREATE_SCHEMA_MIGRATIONS)
            done = {
                record["version"]
                for record in await conn.fetch("SELECT version FROM schema_migrations")
            }
            for version, body in migration_files():
                if version in done:
                    continue
                log.info("applying migration %s", version)
                async with conn.transaction():
                    await conn.execute(body)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                applied.append(version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)
    return applied
