"""The contribution ledger.

Completing a mission moves the holder's stored counters in the same statement as the
mission status (COMPLETE_MISSION / FAIL_MISSION each carry a data-modifying CTE). This
pins the arithmetic: points per completion, success_rate, reputation, what a retryable
failure does *not* touch, and that a replayed complete() does not double-count.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
import pytest_asyncio

from sect.core.app import create_app
from tests.conftest import Sect


async def _complete(sect: Sect, token: str, mission_id: str, claim_token: str) -> httpx.Response:
    return await sect.client.post(
        f"/v1/missions/{mission_id}/complete",
        json={"claim_token": claim_token, "result": {"ok": True}},
        headers=sect.headers(token),
    )


async def test_one_completion_moves_every_counter(sect: Sect) -> None:
    token = await sect.create_disciple("earner", arts=["test"])
    mission = await sect.post_mission()
    claim_token = (await sect.claim(mission["id"], token)).json()["claim_token"]
    assert (await _complete(sect, token, mission["id"], claim_token)).status_code == 200

    record = await sect.disciple("earner")
    assert record["completed_missions"] == 1
    assert record["failed_missions"] == 0
    assert record["contribution_points"] == 1
    assert record["success_rate"] == pytest.approx(1.0)
    assert record["reputation"] == 1
    # The legacy stats block mirrors the stored ledger.
    assert record["stats"]["completed"] == 1
    assert record["stats"]["failed"] == 0


async def test_points_are_one_per_completed_mission(sect: Sect) -> None:
    token = await sect.create_disciple("busy", arts=["test"])
    for _ in range(4):
        mission = await sect.post_mission()
        ct = (await sect.claim(mission["id"], token)).json()["claim_token"]
        await _complete(sect, token, mission["id"], ct)

    record = await sect.disciple("busy")
    assert record["completed_missions"] == 4
    assert record["contribution_points"] == 4
    assert record["reputation"] == 4


async def test_a_terminal_failure_lowers_the_success_rate(sect: Sect) -> None:
    token = await sect.create_disciple("mixed", arts=["test"])
    for _ in range(2):
        mission = await sect.post_mission()
        ct = (await sect.claim(mission["id"], token)).json()["claim_token"]
        await _complete(sect, token, mission["id"], ct)

    doomed = await sect.post_mission(max_attempts=1)
    ct = (await sect.claim(doomed["id"], token)).json()["claim_token"]
    failed = await sect.client.post(
        f"/v1/missions/{doomed['id']}/fail",
        json={"claim_token": ct, "error": "no", "retryable": False},
        headers=sect.headers(token),
    )
    assert failed.json()["status"] == "failed"

    record = await sect.disciple("mixed")
    assert record["completed_missions"] == 2
    assert record["failed_missions"] == 1
    assert record["contribution_points"] == 2  # unchanged: no penalty by default
    assert record["success_rate"] == pytest.approx(2 / 3)
    assert record["reputation"] == 1  # floor(2 * 0.666...)


async def test_a_retryable_failure_is_attributed_to_nobody(sect: Sect) -> None:
    token = await sect.create_disciple("flaky", arts=["test"])
    mission = await sect.post_mission(max_attempts=3)
    ct = (await sect.claim(mission["id"], token)).json()["claim_token"]

    requeued = await sect.client.post(
        f"/v1/missions/{mission['id']}/fail",
        json={"claim_token": ct, "error": "502", "retryable": True, "retry_after_seconds": 0},
        headers=sect.headers(token),
    )
    assert requeued.json()["status"] == "open"

    record = await sect.disciple("flaky")
    assert record["failed_missions"] == 0
    assert record["completed_missions"] == 0
    assert record["contribution_points"] == 0
    assert record["success_rate"] == pytest.approx(0.0)


async def test_a_replayed_completion_does_not_double_count(sect: Sect) -> None:
    token = await sect.create_disciple("retrier", arts=["test"])
    mission = await sect.post_mission()
    claim_token = (await sect.claim(mission["id"], token)).json()["claim_token"]

    first = await _complete(sect, token, mission["id"], claim_token)
    second = await _complete(sect, token, mission["id"], claim_token)
    assert first.status_code == 200
    assert second.status_code == 200

    record = await sect.disciple("retrier")
    assert record["completed_missions"] == 1
    assert record["contribution_points"] == 1


async def test_reputation_tracks_points_times_success_rate(sect: Sect) -> None:
    token = await sect.create_disciple("tracked", arts=["test"])
    for _ in range(5):
        mission = await sect.post_mission()
        ct = (await sect.claim(mission["id"], token)).json()["claim_token"]
        await _complete(sect, token, mission["id"], ct)
    for _ in range(2):
        doomed = await sect.post_mission(max_attempts=1)
        ct = (await sect.claim(doomed["id"], token)).json()["claim_token"]
        await sect.client.post(
            f"/v1/missions/{doomed['id']}/fail",
            json={"claim_token": ct, "error": "no", "retryable": False},
            headers=sect.headers(token),
        )

    record = await sect.disciple("tracked")
    rate = 5 / 7
    assert record["success_rate"] == pytest.approx(rate)
    assert record["reputation"] == int(record["contribution_points"] * rate)  # floor(5 * 5/7) == 3


# --------------------------------------------------------------------------- #
# SECT_FAILURE_POINT_PENALTY
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def penalising_sect(settings, pool) -> Sect:
    app = create_app(settings=replace(settings, failure_point_penalty=2), pool=pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sect.test") as client:
        yield Sect(client, pool)


async def test_a_terminal_failure_can_be_made_to_cost_points(penalising_sect: Sect) -> None:
    sect = penalising_sect
    token = await sect.create_disciple("penalised", arts=["test"])
    for _ in range(3):
        mission = await sect.post_mission()
        ct = (await sect.claim(mission["id"], token)).json()["claim_token"]
        await _complete(sect, token, mission["id"], ct)

    doomed = await sect.post_mission(max_attempts=1)
    ct = (await sect.claim(doomed["id"], token)).json()["claim_token"]
    await sect.client.post(
        f"/v1/missions/{doomed['id']}/fail",
        json={"claim_token": ct, "error": "no", "retryable": False},
        headers=sect.headers(token),
    )

    record = await sect.disciple("penalised")
    assert record["completed_missions"] == 3
    assert record["contribution_points"] == 1  # 3 earned, 2 docked
    assert record["failed_missions"] == 1


async def test_points_never_go_below_zero(penalising_sect: Sect) -> None:
    sect = penalising_sect
    token = await sect.create_disciple("in-the-red", arts=["test"])
    doomed = await sect.post_mission(max_attempts=1)
    ct = (await sect.claim(doomed["id"], token)).json()["claim_token"]
    await sect.client.post(
        f"/v1/missions/{doomed['id']}/fail",
        json={"claim_token": ct, "error": "no", "retryable": False},
        headers=sect.headers(token),
    )
    record = await sect.disciple("in-the-red")
    assert record["contribution_points"] == 0
    assert record["reputation"] == 0
