"""Connection pool and migration runner.

Migrations ship inside the package (``sect/core/migrations``) rather than at the repo
root, because they are applied on boot and therefore have to exist in the wheel and in
the container image.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from itertools import pairwise
from pathlib import Path

import asyncpg

from sect.core.settings import Settings
from sect.env import ensure_loaded

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


async def connect_pool(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 5,
    pgbouncer: bool = False,
) -> asyncpg.Pool:
    """Open a pool from a URL alone.

    Separate from :func:`create_pool` so migrations can run with nothing but
    ``DATABASE_URL`` -- they authenticate nobody, so demanding a master key to apply
    them would be friction for no benefit.
    """
    kwargs: dict[str, object] = {}
    if pgbouncer:
        # Transaction-mode pooling and prepared statements do not mix.
        kwargs["statement_cache_size"] = 0
    pool = await asyncpg.create_pool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
        **kwargs,
    )
    if pool is None:  # pragma: no cover - asyncpg only returns None on misuse
        raise RuntimeError("asyncpg.create_pool returned None")
    return pool


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await connect_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        pgbouncer=settings.db_pgbouncer,
    )


def migration_files() -> list[tuple[str, str]]:
    """Return migrations in numeric order, rejecting duplicate or missing versions."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    numbered = []
    for path in files:
        match = re.match(r"^(\d+)_", path.name)
        if match is None:
            raise ValueError(f"Migration filename must start with NNNN_: {path.name}")
        numbered.append((int(match.group(1)), path))
    numbered.sort(key=lambda item: item[0])
    for previous, current in pairwise(numbered):
        if current[0] == previous[0]:
            raise ValueError(
                f"Duplicate migration version {current[0]:04d}: "
                f"{previous[1].name} and {current[1].name}"
            )
        if current[0] != previous[0] + 1:
            raise ValueError(
                f"Migration version gap after {previous[1].name}: "
                f"expected {previous[0] + 1:04d}, found {current[1].name}"
            )
    return [(path.name, path.read_text(encoding="utf-8")) for _, path in numbered]


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


async def migration_status(pool: asyncpg.Pool) -> list[tuple[str, bool]]:
    """``[(version, applied), ...]`` without changing anything."""
    await pool.execute(CREATE_SCHEMA_MIGRATIONS)
    done = {
        record["version"] for record in await pool.fetch("SELECT version FROM schema_migrations")
    }
    return [(version, version in done) for version, _ in migration_files()]


def _main(argv: list[str] | None = None) -> int:
    """``python -m sect.core.db migrate|status``.

    Needs only ``DATABASE_URL``. Useful for pointing at a new database and confirming
    the schema lands before booting anything against it.
    """
    import argparse
    import asyncio
    import os

    ensure_loaded()

    parser = argparse.ArgumentParser(
        prog="python -m sect.core.db",
        description="Apply or inspect the Sect's schema migrations. Reads DATABASE_URL.",
    )
    parser.add_argument(
        "command",
        choices=("migrate", "status"),
        help="migrate: apply anything pending. status: report without changing anything.",
    )
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2
    pgbouncer = os.environ.get("SECT_DB_PGBOUNCER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    async def run() -> int:
        pool = await connect_pool(database_url, min_size=1, max_size=2, pgbouncer=pgbouncer)
        try:
            if args.command == "migrate":
                applied = await run_migrations(pool)
                for version in applied:
                    print(f"applied  {version}")
                if not applied:
                    print("already up to date")
            else:
                for version, is_applied in await migration_status(pool):
                    print(f"{'applied' if is_applied else 'pending':<8} {version}")
        finally:
            await pool.close()
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(_main())
