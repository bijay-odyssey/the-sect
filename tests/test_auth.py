"""The principal matrix.

Two principals, master and disciple, and a handful of endpoints that insist on one or
the other. This pins who gets in where, and that a deactivated disciple is turned away
everywhere. Referenced by docs/sect-architecture.md as the auth test that was missing
from v0.1.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import MASTER_KEY, Sect

BAD = {"Authorization": "Bearer sect_d_not_a_real_token"}
NONE: dict[str, str] = {}


async def test_master_key_reaches_master_only_endpoints(sect: Sect) -> None:
    created = await sect.client.post(
        "/v1/disciples",
        json={"name": "made-by-master", "arts": ["test"]},
        headers=sect.master_headers,
    )
    assert created.status_code == 201


async def test_disciple_token_is_refused_at_master_only_endpoints(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    token = await sect.create_disciple("plain-disciple")

    resp = await client.post(
        "/v1/disciples", json={"name": "another", "arts": ["test"]}, headers=sect.headers(token)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "master_key_required"

    peak = await client.post(
        "/v1/peaks",
        json={"name": "sneaky-peak", "display_name": "Sneaky", "arts": ["test"]},
        headers=sect.headers(token),
    )
    assert peak.status_code == 403
    assert peak.json()["error"]["code"] == "master_key_required"


async def test_master_key_is_refused_where_an_identity_is_required(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    await sect.post_mission()
    resp = await client.post("/v1/missions/claim-next", headers=sect.master_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "disciple_token_required"


async def test_missing_and_malformed_tokens_are_401(client: httpx.AsyncClient) -> None:
    missing = await client.get("/v1/disciples", headers=NONE)
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "missing_token"

    garbage = await client.get("/v1/disciples", headers=BAD)
    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "invalid_token"


async def test_a_deactivated_disciple_is_turned_away_everywhere(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    token = await sect.create_disciple("soon-inactive", arts=["test"])
    patched = await client.patch(
        "/v1/disciples/soon-inactive", json={"active": False}, headers=sect.master_headers
    )
    assert patched.status_code == 200

    for method, path in [
        ("GET", "/v1/disciples"),
        ("PUT", "/v1/disciples/me"),
        ("POST", "/v1/missions/claim-next"),
    ]:
        resp = await client.request(
            method, path, json={} if method != "GET" else None, headers=sect.headers(token)
        )
        assert resp.status_code == 401, (path, resp.text)
        assert resp.json()["error"]["code"] == "disciple_inactive"


async def test_any_authenticated_principal_may_read_shared_endpoints(
    sect: Sect, client: httpx.AsyncClient
) -> None:
    token = await sect.create_disciple("reader")
    for headers in (sect.master_headers, sect.headers(token)):
        assert (await client.get("/v1/disciples", headers=headers)).status_code == 200
        assert (await client.get("/v1/stats", headers=headers)).status_code == 200
        assert (await client.get("/v1/peaks", headers=headers)).status_code == 200


async def test_health_needs_no_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize("key", [MASTER_KEY, "obviously-wrong"])
async def test_master_key_comparison_is_all_or_nothing(client: httpx.AsyncClient, key: str) -> None:
    resp = await client.post(
        "/v1/peaks",
        json={"name": "probe-peak", "display_name": "Probe", "arts": ["test"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == (201 if key == MASTER_KEY else 401)
