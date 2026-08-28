"""The drift guard docs/sect-architecture.md and sect/realms.py both promise.

``sect.realms.Realm`` (a typing.Literal) and the ``CHECK`` on ``disciples.realm`` are
maintained by hand in two files. If they ever disagree -- a realm added to one and not
the other -- a disciple could be granted a realm the database rejects, or vice versa.
This test fails loudly the moment they diverge. It also covers the ``peaks.status``
enum, which has the same shape and the same risk.
"""

from __future__ import annotations

import re
from typing import get_args

import asyncpg

from sect.models import PeakStatus
from sect.realms import REALMS, STARTING_REALM

_QUOTED = re.compile(r"'([^']*)'")


async def _check_literals(pool: asyncpg.Pool, table: str, column: str) -> set[str]:
    """The string set a column's CHECK constraint allows, read back from the catalog."""
    definition = await pool.fetchval(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint AS c
        JOIN pg_class AS t ON t.oid = c.conrelid
        JOIN pg_attribute AS a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
        WHERE t.relname = $1 AND a.attname = $2 AND c.contype = 'c'
        """,
        table,
        column,
    )
    assert definition, f"no CHECK constraint found on {table}.{column}"
    return {token.replace("::text", "") for token in _QUOTED.findall(definition)}


async def _column_default(pool: asyncpg.Pool, table: str, column: str) -> str | None:
    raw = await pool.fetchval(
        """
        SELECT column_default
        FROM information_schema.columns
        WHERE table_name = $1 AND column_name = $2
        """,
        table,
        column,
    )
    match = _QUOTED.search(raw or "")
    return match.group(1) if match else None


async def test_realm_ladder_matches_the_database_check(pool: asyncpg.Pool) -> None:
    allowed = await _check_literals(pool, "disciples", "realm")
    assert allowed == set(REALMS), (
        f"sect.realms.REALMS is {sorted(REALMS)} but the disciples.realm CHECK allows "
        f"{sorted(allowed)} -- update whichever migration or Literal is behind"
    )


async def test_starting_realm_is_the_column_default(pool: asyncpg.Pool) -> None:
    assert await _column_default(pool, "disciples", "realm") == STARTING_REALM


async def test_peak_status_enum_matches_the_database_check(pool: asyncpg.Pool) -> None:
    allowed = await _check_literals(pool, "peaks", "status")
    assert allowed == set(get_args(PeakStatus))


async def test_peak_status_default_is_active(pool: asyncpg.Pool) -> None:
    assert await _column_default(pool, "peaks", "status") == "active"
