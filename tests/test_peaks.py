"""The Peak System: registry CRUD, plus the routing rule that a peak is not a wall.

A mission tagged with a peak stays claimable by any disciple whose arts match -- peak
members, other peaks' disciples, and wandering cultivators alike. ``peak_id`` only
changes the *order* work is offered in: a disciple sees its own peak's missions first.
"""

from __future__ import annotations

import httpx

from tests.conftest import Sect

# --------------------------------------------------------------------------- #
# Registry CRUD
# --------------------------------------------------------------------------- #


async def test_master_registers_a_peak_and_a_duplicate_name_is_rejected(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    created = await client.post(
        "/v1/peaks",
        json={
            "name": "scraping-peak",
            "display_name": "Web Scraping Peak",
            "description": "Fetches and parses the open web.",
            "arts": ["web_scraping", "html_parsing"],
        },
        headers=sect.master_headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "scraping-peak"
    assert body["status"] == "active"
    assert sorted(body["arts"]) == ["html_parsing", "web_scraping"]
    assert body["stats"] == {"disciples": 0, "completed_missions": 0}

    again = await client.post(
        "/v1/peaks",
        json={"name": "scraping-peak", "display_name": "Dup", "arts": []},
        headers=sect.master_headers,
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "peak_exists"


async def test_a_disciple_cannot_register_a_peak(sect: Sect, client: httpx.AsyncClient) -> None:
    token = await sect.create_disciple("nobody")
    resp = await client.post(
        "/v1/peaks",
        json={"name": "forbidden-peak", "display_name": "No", "arts": []},
        headers=sect.headers(token),
    )
    assert resp.status_code == 403


async def test_list_filters_by_art_and_by_status(sect: Sect, client: httpx.AsyncClient) -> None:
    await sect.create_peak("alpha-peak", arts=["summarize"])
    await sect.create_peak("beta-peak", arts=["transcode"])
    await sect.create_peak("gamma-peak", arts=["summarize"])

    everything = await client.get("/v1/peaks", headers=sect.master_headers)
    assert {p["name"] for p in everything.json()["peaks"]} == {
        "alpha-peak",
        "beta-peak",
        "gamma-peak",
    }

    summarizers = await client.get("/v1/peaks?art=summarize", headers=sect.master_headers)
    assert {p["name"] for p in summarizers.json()["peaks"]} == {"alpha-peak", "gamma-peak"}

    await client.delete("/v1/peaks/beta-peak", headers=sect.master_headers)
    active = await client.get("/v1/peaks", headers=sect.master_headers)
    assert "beta-peak" not in {p["name"] for p in active.json()["peaks"]}
    inactive = await client.get("/v1/peaks?status=inactive", headers=sect.master_headers)
    assert {p["name"] for p in inactive.json()["peaks"]} == {"beta-peak"}


async def test_get_unknown_peak_is_404(sect: Sect, client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/peaks/ghost-peak", headers=sect.master_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "peak_not_found"


async def test_patch_updates_metadata_and_status(sect: Sect, client: httpx.AsyncClient) -> None:
    await sect.create_peak("mut-peak", arts=["a"])
    patched = await client.patch(
        "/v1/peaks/mut-peak",
        json={"arts": ["a", "b"], "description": "now does b too", "status": "suspended"},
        headers=sect.master_headers,
    )
    assert patched.status_code == 200
    body = patched.json()
    assert sorted(body["arts"]) == ["a", "b"]
    assert body["status"] == "suspended"
    assert body["description"] == "now does b too"


async def test_delete_is_soft_and_keeps_the_record(sect: Sect, client: httpx.AsyncClient) -> None:
    await sect.create_peak("doomed-peak")
    deleted = await client.delete("/v1/peaks/doomed-peak", headers=sect.master_headers)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "inactive"
    # Still addressable by name -- the history is not thrown away.
    assert (
        await client.get("/v1/peaks/doomed-peak", headers=sect.master_headers)
    ).status_code == 200


async def test_heartbeat_records_last_seen(sect: Sect, client: httpx.AsyncClient) -> None:
    await sect.create_peak("live-peak")
    assert (await client.get("/v1/peaks/live-peak", headers=sect.master_headers)).json()[
        "last_seen_at"
    ] is None
    beat = await client.post("/v1/peaks/live-peak/heartbeat", headers=sect.master_headers)
    assert beat.status_code == 200
    assert beat.json()["last_seen_at"] is not None


async def test_peak_stats_roll_up_disciples_and_their_completions(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    await sect.create_peak("counted-peak", arts=["test"])
    token = await sect.create_disciple("counted-one", arts=["test"], peak="counted-peak")
    mission = await sect.post_mission()
    claim = await sect.claim(mission["id"], token)
    await client.post(
        f"/v1/missions/{mission['id']}/complete",
        json={"claim_token": claim.json()["claim_token"], "result": {}},
        headers=sect.headers(token),
    )

    peak = await client.get("/v1/peaks/counted-peak", headers=sect.master_headers)
    assert peak.json()["stats"] == {"disciples": 1, "completed_missions": 1}


# --------------------------------------------------------------------------- #
# Routing: a peak is not a wall
# --------------------------------------------------------------------------- #


async def test_a_peak_tagged_mission_is_claimable_by_anyone_with_the_art(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    await sect.create_peak("home-peak", arts=["testing"])
    await sect.create_peak("other-peak", arts=["testing"])

    member = await sect.create_disciple("member", arts=["testing"], peak="home-peak")
    outsider = await sect.create_disciple("outsider", arts=["testing"], peak="other-peak")
    wanderer = await sect.create_disciple("wanderer", arts=["testing"])

    for who in (member, outsider, wanderer):
        mission = await sect.post_mission(art="testing", peak="home-peak")
        listing = await client.get("/v1/missions/open", headers=sect.headers(who))
        assert mission["id"] in {m["id"] for m in listing.json()["missions"]}
        claim = await sect.claim(mission["id"], who)
        assert claim.status_code == 200, (who, claim.text)


async def test_claim_next_offers_a_disciple_its_own_peaks_work_first(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    await sect.create_peak("mine-peak", arts=["testing"])
    mine = await sect.create_disciple("mine-cultivator", arts=["testing"], peak="mine-peak")
    drifter = await sect.create_disciple("drifter", arts=["testing"])

    # The general mission is older, so on age alone it would be dealt first.
    general = await sect.post_mission(art="testing", title="general")
    mine_mission = await sect.post_mission(art="testing", title="mine", peak="mine-peak")

    # The peak member is pulled toward its own peak despite the general one being older.
    got = await client.post("/v1/missions/claim-next", headers=sect.headers(mine))
    assert got.json()["mission"]["id"] == mine_mission["id"]

    # The drifter has no peak, so it just takes the oldest claimable mission.
    drift_got = await client.post("/v1/missions/claim-next", headers=sect.headers(drifter))
    assert drift_got.json()["mission"]["id"] == general["id"]


async def test_open_board_sorts_a_disciples_own_peak_to_the_top(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    await sect.create_peak("top-peak", arts=["testing"])
    who = await sect.create_disciple("sorter", arts=["testing"], peak="top-peak")

    await sect.post_mission(art="testing", title="first-posted")
    peak_mission = await sect.post_mission(art="testing", title="second-posted", peak="top-peak")

    listing = await client.get("/v1/missions/open", headers=sect.headers(who))
    assert listing.json()["missions"][0]["id"] == peak_mission["id"]


async def test_posting_to_an_unknown_peak_is_rejected(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    resp = await client.post(
        "/v1/missions",
        json={"title": "orphan", "required_art": "test", "peak": "no-such-peak"},
        headers=sect.master_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "peak_not_found"


async def test_a_disciple_may_join_and_leave_a_peak_itself(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    await sect.create_peak("join-peak", arts=["test"])
    token = await sect.create_disciple("joiner", arts=["test"])
    assert (await sect.disciple("joiner"))["peak"] is None

    joined = await client.put(
        "/v1/disciples/me", json={"peak": "join-peak"}, headers=sect.headers(token)
    )
    assert joined.status_code == 200
    assert joined.json()["peak"] == "join-peak"

    left = await client.put("/v1/disciples/me", json={"peak": None}, headers=sect.headers(token))
    assert left.json()["peak"] is None
