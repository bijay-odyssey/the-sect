"""What happens when a disciple dies mid-mission.

This is the failure mode the GitHub Actions deployment actually hits: a runner is
killed, times out, or loses its network, and nothing is ever reported back. The Sect
has to recover the work without letting the dead disciple corrupt it if it wakes up.
"""

from __future__ import annotations

import httpx

from tests.conftest import Sect


async def test_an_expired_lease_returns_the_mission_to_the_board(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    first = await sect.create_disciple("vanisher")
    second = await sect.create_disciple("successor")
    mission = await sect.post_mission()

    await sect.claim(mission["id"], first)
    assert (await sect.claim(mission["id"], second)).status_code == 409

    await sect.expire_lease(mission["id"])

    listing = await client.get("/v1/missions/open", headers=sect.headers(second))
    assert listing.json()["count"] == 1, "an expired lease should make work visible again"

    takeover = await sect.claim(mission["id"], second)
    assert takeover.status_code == 200

    row = await sect.mission_row(mission["id"])
    assert row["attempts"] == 2, "the abandoned run should count against the retry budget"
    assert row["claimed_by"] == await sect.disciple_id("successor")


async def test_a_stale_holder_cannot_overwrite_the_disciple_that_redid_the_work(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    """The scenario claim_token exists for.

    First disciple stalls, loses its lease, second disciple redoes the mission and
    reports. Then the first wakes up and tries to report its own stale result.
    """
    first = await sect.create_disciple("staller")
    second = await sect.create_disciple("finisher")
    mission = await sect.post_mission()
    mission_id = mission["id"]

    stale_token = (await sect.claim(mission_id, first)).json()["claim_token"]
    await sect.expire_lease(mission_id)
    fresh_token = (await sect.claim(mission_id, second)).json()["claim_token"]
    assert fresh_token != stale_token

    good = await client.post(
        f"/v1/missions/{mission_id}/complete",
        json={"claim_token": fresh_token, "result": {"by": "finisher"}},
        headers=sect.headers(second),
    )
    assert good.status_code == 200

    late = await client.post(
        f"/v1/missions/{mission_id}/complete",
        json={"claim_token": stale_token, "result": {"by": "staller"}},
        headers=sect.headers(first),
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "not_mission_holder"
    assert late.json()["error"]["detail"]["reason"] == "already_completed"

    row = await sect.mission_row(mission_id)
    assert row["result"] == {"by": "finisher"}, "the late report must not win"


async def test_a_stale_holder_cannot_fail_a_mission_someone_else_now_holds(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    first = await sect.create_disciple("quitter")
    second = await sect.create_disciple("worker")
    mission = await sect.post_mission()
    mission_id = mission["id"]

    stale_token = (await sect.claim(mission_id, first)).json()["claim_token"]
    await sect.expire_lease(mission_id)
    await sect.claim(mission_id, second)

    late = await client.post(
        f"/v1/missions/{mission_id}/fail",
        json={"claim_token": stale_token, "error": "gave up", "retryable": False},
        headers=sect.headers(first),
    )
    assert late.status_code == 409
    assert late.json()["error"]["detail"]["reason"] == "reclaimed"

    row = await sect.mission_row(mission_id)
    assert row["status"] == "claimed", "someone else's run must not be cancelled out"


async def test_the_sweep_fails_a_mission_that_has_no_attempts_left(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    """A mission at max_attempts with a dead lease can never be claimed again.

    Without the sweep it would sit in 'claimed' forever and the board would lie.
    """
    token = await sect.create_disciple("one-shot")
    mission = await sect.post_mission(max_attempts=1)

    await sect.claim(mission["id"], token)
    await sect.expire_lease(mission["id"])

    swept = await client.post("/v1/admin/sweep", headers=sect.master_headers)
    assert swept.status_code == 200
    assert swept.json()["swept"] == 1

    row = await sect.mission_row(mission["id"])
    assert row["status"] == "failed"
    assert "attempts exhausted" in row["error"]
    assert row["finished_at"] is not None


async def test_polling_sweeps_zombies_without_anyone_asking(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    """No scheduler exists, so the sweep has to ride along on ordinary traffic."""
    token = await sect.create_disciple("passer-by")
    mission = await sect.post_mission(max_attempts=1)
    await sect.claim(mission["id"], token)
    await sect.expire_lease(mission["id"])

    await client.get("/v1/missions/open", headers=sect.headers(token))

    row = await sect.mission_row(mission["id"])
    assert row["status"] == "failed"


async def test_completing_twice_is_a_replay_not_a_conflict(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    """complete() must be safe to retry when a response goes missing."""
    token = await sect.create_disciple("retrier")
    mission = await sect.post_mission()
    claim_token = (await sect.claim(mission["id"], token)).json()["claim_token"]

    body = {"claim_token": claim_token, "result": {"ok": True}}
    first = await client.post(
        f"/v1/missions/{mission['id']}/complete", json=body, headers=sect.headers(token)
    )
    second = await client.post(
        f"/v1/missions/{mission['id']}/complete", json=body, headers=sect.headers(token)
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert second.json()["result"] == {"ok": True}


async def test_heartbeat_holds_off_a_takeover(sect: Sect, client: httpx.AsyncClient) -> None:
    """A long-running disciple should be able to keep work it is still doing."""
    holder = await sect.create_disciple("slow-but-alive")
    rival = await sect.create_disciple("opportunist")
    mission = await sect.post_mission()

    claim_token = (await sect.claim(mission["id"], holder)).json()["claim_token"]
    await sect.expire_lease(mission["id"])

    beat = await client.post(
        f"/v1/missions/{mission['id']}/heartbeat",
        json={"claim_token": claim_token, "extend_seconds": 600},
        headers=sect.headers(holder),
    )
    assert beat.status_code == 200

    assert (await sect.claim(mission["id"], rival)).status_code == 409


async def test_a_retryable_failure_goes_back_on_the_board_after_its_backoff(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    token = await sect.create_disciple("flaky")
    mission = await sect.post_mission(max_attempts=3)
    claim_token = (await sect.claim(mission["id"], token)).json()["claim_token"]

    failed = await client.post(
        f"/v1/missions/{mission['id']}/fail",
        json={
            "claim_token": claim_token,
            "error": "upstream 502",
            "retryable": True,
            "retry_after_seconds": 0,
        },
        headers=sect.headers(token),
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "open"
    assert failed.json()["claimed_by"] is None, "a requeued mission belongs to nobody"

    row = await sect.mission_row(mission["id"])
    assert row["attempts"] == 1
    assert row["claim_token"] is None

    assert (await sect.claim(mission["id"], token)).status_code == 200


async def test_a_terminal_failure_stays_failed(sect: Sect, client: httpx.AsyncClient) -> None:
    token = await sect.create_disciple("honest")
    mission = await sect.post_mission(max_attempts=5)
    claim_token = (await sect.claim(mission["id"], token)).json()["claim_token"]

    failed = await client.post(
        f"/v1/missions/{mission['id']}/fail",
        json={
            "claim_token": claim_token,
            "error": "payload is nonsense, retrying will not help",
            "retryable": False,
        },
        headers=sect.headers(token),
    )
    assert failed.json()["status"] == "failed"
    assert (await sect.claim(mission["id"], token)).status_code == 409


async def test_the_last_attempt_failing_retryably_is_still_terminal(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    """retryable=True is a request, not a guarantee: max_attempts wins."""
    token = await sect.create_disciple("persistent")
    mission = await sect.post_mission(max_attempts=1)
    claim_token = (await sect.claim(mission["id"], token)).json()["claim_token"]

    failed = await client.post(
        f"/v1/missions/{mission['id']}/fail",
        json={"claim_token": claim_token, "error": "boom", "retryable": True},
        headers=sect.headers(token),
    )
    assert failed.json()["status"] == "failed"
