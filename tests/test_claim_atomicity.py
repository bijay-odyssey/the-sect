"""The tests the whole design rests on.

If any of these go red, the Sect is not safe to run two disciples against.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import asyncpg
import httpx
import pytest

from sect.core import sql
from tests.conftest import TEST_DATABASE_URL, Sect

CONCURRENCY = 20


async def test_concurrent_claims_at_sql_level_produce_exactly_one_winner(
    sect: Sect, pool: asyncpg.Pool
) -> None:
    """The claim statement itself, with no HTTP layer in the way.

    Twenty separate connections issue CLAIM_MISSION against one open mission at the
    same moment. Nineteen of them block on the row lock, then re-evaluate their WHERE
    clause against the newly committed row, match nothing, and return zero rows.
    """
    for index in range(CONCURRENCY):
        await sect.create_disciple(f"claimer-{index:02d}")
    disciple_ids = [
        record["id"] for record in await pool.fetch("SELECT id FROM disciples ORDER BY name")
    ]
    mission = await sect.post_mission()
    mission_id = UUID(mission["id"])

    # Each claim needs its own connection, otherwise they queue instead of racing.
    connections = await asyncio.gather(
        *(asyncpg.connect(TEST_DATABASE_URL) for _ in range(CONCURRENCY))
    )
    try:
        results = await asyncio.gather(
            *(
                connection.fetchrow(sql.CLAIM_MISSION, mission_id, disciple_id, None)
                for connection, disciple_id in zip(connections, disciple_ids, strict=True)
            )
        )
    finally:
        await asyncio.gather(*(connection.close() for connection in connections))

    winners = [row for row in results if row is not None]
    assert len(winners) == 1, f"{len(winners)} disciples claimed the same mission"

    row = await sect.mission_row(mission["id"])
    assert row["status"] == "claimed"
    assert row["attempts"] == 1, "a lost claim must not burn an attempt"
    assert row["claimed_by"] == winners[0]["claimed_by_id"]


async def test_concurrent_claims_over_http_produce_exactly_one_winner(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    """The same race, through the API, which is how it will actually happen."""
    tokens = [await sect.create_disciple(f"racer-{index:02d}") for index in range(CONCURRENCY)]
    mission = await sect.post_mission()

    responses = await asyncio.gather(*(sect.claim(mission["id"], token) for token in tokens))
    statuses = [response.status_code for response in responses]

    assert statuses.count(200) == 1
    assert statuses.count(409) == CONCURRENCY - 1

    losers = [response for response in responses if response.status_code == 409]
    assert all(r.json()["error"]["code"] == "mission_not_claimable" for r in losers)
    assert all(r.json()["error"]["detail"]["status"] == "claimed" for r in losers)

    row = await sect.mission_row(mission["id"])
    assert row["attempts"] == 1


async def test_claim_next_deals_every_disciple_a_different_mission(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    """Twenty disciples, twenty missions, one simultaneous rush at the board.

    FOR UPDATE SKIP LOCKED means nobody waits and nobody collides: the assignment
    should come out exactly 1:1 with no retries anywhere.
    """
    tokens = [await sect.create_disciple(f"eager-{index:02d}") for index in range(CONCURRENCY)]
    for index in range(CONCURRENCY):
        await sect.post_mission(title=f"mission {index}")

    responses = await asyncio.gather(
        *(client.post("/v1/missions/claim-next", headers=sect.headers(token)) for token in tokens)
    )

    assert all(response.status_code == 200 for response in responses), [
        r.status_code for r in responses
    ]
    claimed_ids = [response.json()["mission"]["id"] for response in responses]
    assert len(set(claimed_ids)) == CONCURRENCY, "two disciples were dealt the same mission"

    holders = await sect.pool.fetchval(
        "SELECT count(DISTINCT claimed_by) FROM missions WHERE status = 'claimed'"
    )
    assert holders == CONCURRENCY


async def test_claim_next_returns_204_when_the_board_is_empty(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    token = await sect.create_disciple("idle")
    response = await client.post("/v1/missions/claim-next", headers=sect.headers(token))
    assert response.status_code == 204


async def test_reclaiming_your_own_mission_is_idempotent(sect: Sect) -> None:
    """A lost HTTP response must be recoverable.

    Re-claiming a mission you already hold returns the same claim_token and does not
    count as a fresh attempt -- otherwise a flaky connection would silently eat the
    retry budget of every mission.
    """
    token = await sect.create_disciple("stubborn")
    mission = await sect.post_mission()

    first = await sect.claim(mission["id"], token)
    second = await sect.claim(mission["id"], token)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["claim_token"] == second.json()["claim_token"]

    row = await sect.mission_row(mission["id"])
    assert row["attempts"] == 1


async def test_claim_next_respects_priority_then_age(sect: Sect, client: httpx.AsyncClient) -> None:
    token = await sect.create_disciple("picky")
    await sect.post_mission(title="ordinary", priority=0)
    urgent = await sect.post_mission(title="urgent", priority=10)

    response = await client.post("/v1/missions/claim-next", headers=sect.headers(token))
    assert response.json()["mission"]["id"] == urgent["id"]


async def test_a_disciple_is_never_dealt_work_outside_its_arts(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    token = await sect.create_disciple("scribe", arts=["summarize"])
    await sect.post_mission(art="transcode")

    claim = await client.post("/v1/missions/claim-next", headers=sect.headers(token))
    assert claim.status_code == 204

    listing = await client.get("/v1/missions/open", headers=sect.headers(token))
    assert listing.json()["missions"] == []


@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_a_finished_mission_cannot_be_claimed(
    sect: Sect, client: httpx.AsyncClient, status: str
) -> None:
    token = await sect.create_disciple("latecomer")
    mission = await sect.post_mission()

    if status == "completed":
        claim = await sect.claim(mission["id"], token)
        await client.post(
            f"/v1/missions/{mission['id']}/complete",
            json={"claim_token": claim.json()["claim_token"], "result": {}},
            headers=sect.headers(token),
        )
    else:
        await client.post(f"/v1/missions/{mission['id']}/cancel", headers=sect.master_headers)

    again = await sect.claim(mission["id"], token)
    assert again.status_code == 409
    assert again.json()["error"]["detail"]["status"] == status


async def test_a_mission_scheduled_for_later_is_not_claimable_yet(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    token = await sect.create_disciple("early-bird")
    mission = await sect.post_mission(not_before="2999-01-01T00:00:00Z")

    assert (await sect.claim(mission["id"], token)).status_code == 409

    listing = await client.get("/v1/missions/open", headers=sect.headers(token))
    assert listing.json()["count"] == 0
