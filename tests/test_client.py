"""SDK tests, against a real uvicorn over real HTTP.

The ones that matter here are the retry tests. Endpoint-level idempotency was already
proved in test_claim_atomicity and test_lease_expiry by calling the endpoint twice by
hand; what is proved here is the thing that actually happens in production -- the SDK
retrying *by itself*, into those endpoints, after a response goes missing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

import httpx
import pytest

from sect.client import Disciple, SectMaster
from sect.errors import AuthError, MissionNotClaimable, PermanentFailure, SectUnavailable
from sect.models import Mission
from tests.conftest import MASTER_KEY, Db

# Keep the backoff out of the wall clock; the policy is what is under test, not the nap.
FAST_BACKOFF = {"backoff_base": 0.01, "backoff_cap": 0.05}


class LosesResponses(httpx.BaseTransport):
    """Lets the server do the work, then throws the response away.

    This is the failure that makes idempotency necessary, and it is *not* a refused
    connection: the mission really was claimed, or really was completed, and the client
    has no idea. A client that retries into a non-idempotent endpoint corrupts state
    here; one that retries into an idempotent endpoint recovers.
    """

    def __init__(self, drop_first: int, path_contains: str | None = None) -> None:
        self._inner = httpx.HTTPTransport()
        self._drop_first = drop_first
        self._path_contains = path_contains
        self.attempts = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        watched = self._path_contains is None or self._path_contains in request.url.path
        if watched:
            self.attempts += 1
        response = self._inner.handle_request(request)
        if watched and self.attempts <= self._drop_first:
            response.read()
            response.close()
            raise httpx.ReadTimeout("simulated: response lost in transit", request=request)
        return response

    def close(self) -> None:
        self._inner.close()


class ColdStart(httpx.BaseTransport):
    """Answers 503 a few times, the way a host still waking up does."""

    def __init__(self, unavailable_times: int) -> None:
        self._inner = httpx.HTTPTransport()
        self._remaining = unavailable_times
        self.attempts = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            return httpx.Response(
                503,
                json={"error": {"code": "unavailable", "message": "waking up"}},
                request=request,
            )
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


@pytest.fixture
def master(live_server: str, clean_db: None) -> Iterator[SectMaster]:
    with SectMaster(live_server, MASTER_KEY, **FAST_BACKOFF) as client:
        yield client


DiscipleFactory = Callable[..., Disciple]


@pytest.fixture
def make_disciple(live_server: str, master: SectMaster) -> Iterator[DiscipleFactory]:
    made: list[Disciple] = []

    def make(
        name: str = "scribe",
        arts: Sequence[str] = ("summarize",),
        **kwargs: object,
    ) -> Disciple:
        _, token = master.register_disciple(name, list(arts))
        disciple = Disciple(
            name,
            list(arts),
            base_url=live_server,
            token=token,
            **FAST_BACKOFF,
            **kwargs,  # type: ignore[arg-type]
        )
        made.append(disciple)
        return disciple

    yield make
    for disciple in made:
        disciple.close()


# --------------------------------------------------------------------------- #
# The retry path -- the SDK driving itself into idempotent endpoints
# --------------------------------------------------------------------------- #


def test_sdk_retries_a_lost_claim_response_without_burning_an_attempt(
    master: SectMaster, make_disciple: DiscipleFactory, db: Db
) -> None:
    """Two claim responses vanish. The SDK retries, and the mission still shows one try.

    Without the server's retry-safe re-claim branch this would end with the mission
    claimed at attempts=3, two-thirds of its retry budget spent on a network problem.
    """
    mission = master.post_mission("Summarize the week", "summarize")
    transport = LosesResponses(drop_first=2, path_contains="/claim")
    disciple = make_disciple("retrier", transport=transport)

    claimed = disciple.claim(mission.id)

    assert transport.attempts == 3, "expected two lost responses and one delivered"
    assert claimed.id == mission.id

    row = db.mission(mission.id)
    assert row["status"] == "claimed"
    assert row["attempts"] == 1, "a lost response must not count as a failed attempt"

    # The only way this succeeds is if the token the SDK kept is the one the server
    # stored -- i.e. the retry recovered the original claim rather than making a new one.
    assert disciple.complete(mission.id, {"ok": True}).status == "completed"


def test_sdk_retries_a_lost_complete_response_into_the_replay_path(
    master: SectMaster, make_disciple: DiscipleFactory, db: Db
) -> None:
    """The result was already stored; the SDK just never heard back.

    The retry must land on 200 (an idempotent replay), not 409 -- otherwise every flaky
    connection turns finished work into an apparent failure.
    """
    mission = master.post_mission("Summarize the week", "summarize")
    transport = LosesResponses(drop_first=1, path_contains="/complete")
    disciple = make_disciple("finisher", transport=transport)

    disciple.claim(mission.id)
    finished = disciple.complete(mission.id, {"summary": "seven commits, one revert"})

    assert transport.attempts == 2
    assert finished.status == "completed"
    assert finished.result == {"summary": "seven commits, one revert"}
    assert db.mission(mission.id)["result"] == {"summary": "seven commits, one revert"}


def test_sdk_retries_a_lost_claim_next_response(
    master: SectMaster, make_disciple: DiscipleFactory, db: Db
) -> None:
    """claim-next is the one call that is *not* idempotent, and the SDK retries it anyway.

    A lost claim-next response means the mission is held by a disciple that does not
    know it. The retry takes the next mission instead, and the orphan is recovered by
    its lease -- which is the whole reason leases exist. What must not happen is the
    orphan being lost silently or the retry raising.
    """
    for index in range(2):
        master.post_mission(f"mission {index}", "summarize")
    transport = LosesResponses(drop_first=1, path_contains="claim-next")
    disciple = make_disciple("grabby", transport=transport)

    mission = disciple.claim_next()

    assert transport.attempts == 2
    assert mission is not None
    assert disciple.complete(mission.id, {"done": True}).status == "completed"

    orphaned = db.fetchval("SELECT count(*) FROM missions WHERE status = 'claimed'")
    assert orphaned == 1, "the first claim is held by nobody, awaiting its lease"


def test_sdk_retries_while_the_host_is_waking_up(
    master: SectMaster, make_disciple: DiscipleFactory
) -> None:
    """Three 503s then success -- the free-tier cold start, compressed."""
    mission = master.post_mission("Summarize the week", "summarize")
    transport = ColdStart(unavailable_times=3)
    disciple = make_disciple("patient", transport=transport)

    claimed = disciple.claim(mission.id)

    assert transport.attempts == 4
    assert claimed.id == mission.id


def test_sdk_gives_up_after_max_retries(master: SectMaster, make_disciple: DiscipleFactory) -> None:
    mission = master.post_mission("Summarize the week", "summarize")
    transport = ColdStart(unavailable_times=99)
    disciple = make_disciple("doomed", transport=transport, max_retries=2)

    with pytest.raises(SectUnavailable):
        disciple.claim(mission.id)
    assert transport.attempts == 3, "one attempt plus two retries, then stop"


def test_sdk_does_not_retry_a_deterministic_conflict(
    master: SectMaster, make_disciple: DiscipleFactory
) -> None:
    """Losing a race is an answer, not a hiccup. Retrying it wastes a runner's minutes."""
    mission = master.post_mission("Summarize the week", "summarize")
    winner = make_disciple("winner")
    transport = LosesResponses(drop_first=0, path_contains="/claim")
    loser = make_disciple("loser", transport=transport)

    winner.claim(mission.id)
    with pytest.raises(MissionNotClaimable) as caught:
        loser.claim(mission.id)

    assert transport.attempts == 1, "a 409 must not be retried"
    assert caught.value.detail["claimed_by"] == "winner"


# --------------------------------------------------------------------------- #
# run_once -- the whole body of a scheduled disciple
# --------------------------------------------------------------------------- #


def test_run_once_claims_runs_and_reports(
    master: SectMaster, make_disciple: DiscipleFactory
) -> None:
    mission = master.post_mission(
        "Summarize the week", "summarize", payload={"text": "one two three"}
    )
    disciple = make_disciple()

    finished = disciple.run_once(lambda m: {"words": len(m.payload["text"].split())})

    assert finished is not None
    assert finished.id == mission.id
    assert finished.status == "completed"
    assert finished.result == {"words": 3}
    assert master.mission(mission.id).result == {"words": 3}


def test_run_once_returns_none_on_an_empty_board(make_disciple: DiscipleFactory) -> None:
    assert make_disciple().run_once(lambda m: {"never": "called"}) is None


def test_run_once_reports_a_crash_as_a_retryable_failure(
    master: SectMaster, make_disciple: DiscipleFactory
) -> None:
    master.post_mission("Summarize the week", "summarize", max_attempts=3)
    disciple = make_disciple()

    def explode(mission: Mission) -> dict[str, str]:
        raise ValueError("the payload made no sense")

    finished = disciple.run_once(explode)

    assert finished is not None
    assert finished.status == "open", "a crash should put the mission back on the board"
    assert "ValueError: the payload made no sense" in finished.error
    assert "Traceback" in finished.error, "the traceback is the point of reporting it"


def test_run_once_honours_permanent_failure(
    master: SectMaster, make_disciple: DiscipleFactory
) -> None:
    """A handler that knows retrying is pointless should not burn three runs proving it."""
    master.post_mission("Summarize the week", "summarize", max_attempts=5)
    disciple = make_disciple()

    def refuse(mission: Mission) -> dict[str, str]:
        raise PermanentFailure("payload has no text field; a retry cannot fix that")

    finished = disciple.run_once(refuse)

    assert finished is not None
    assert finished.status == "failed"
    assert finished.error == "payload has no text field; a retry cannot fix that"


def test_run_once_registers_the_disciple_on_the_way_past(
    master: SectMaster, make_disciple: DiscipleFactory
) -> None:
    disciple = make_disciple("announcer", agent_version="2026.08.12+abc1234")
    assert master.disciple("announcer").last_seen_at is None

    disciple.run_once(lambda m: None)

    record = master.disciple("announcer")
    assert record.last_seen_at is not None
    assert record.agent_version == "2026.08.12+abc1234"


# --------------------------------------------------------------------------- #
# Everything else the SDK promises
# --------------------------------------------------------------------------- #


def test_poll_missions_defaults_to_the_disciples_own_arts(
    master: SectMaster, make_disciple: DiscipleFactory
) -> None:
    master.post_mission("summarize this", "summarize")
    master.post_mission("transcode that", "transcode")
    disciple = make_disciple("narrow", arts=["summarize"])

    missions = disciple.poll_missions()

    assert [m.required_art for m in missions] == ["summarize"]


def test_heartbeat_extends_the_lease(master: SectMaster, make_disciple: DiscipleFactory) -> None:
    mission = master.post_mission("Summarize the week", "summarize", lease_seconds=60)
    disciple = make_disciple()

    claimed = disciple.claim(mission.id)
    extended = disciple.heartbeat(mission.id, extend_seconds=600)

    assert claimed.lease_expires_at is not None
    assert extended > claimed.lease_expires_at


def test_completing_a_mission_you_never_claimed_fails_before_the_network(
    make_disciple: DiscipleFactory, master: SectMaster
) -> None:
    """A claim token lives only in the process that won the claim."""
    mission = master.post_mission("Summarize the week", "summarize")
    disciple = make_disciple()

    with pytest.raises(Exception, match="holds no claim token"):
        disciple.complete(mission.id, {"ok": True})


def test_a_bad_token_raises_auth_error(live_server: str, clean_db: None) -> None:
    with (
        Disciple(
            "impostor", ["summarize"], base_url=live_server, token="sect_d_nonsense", **FAST_BACKOFF
        ) as disciple,
        pytest.raises(AuthError),
    ):
        disciple.register()


def test_idempotency_key_makes_posting_a_mission_replayable(master: SectMaster) -> None:
    first = master.post_mission("Weekly digest", "summarize", idempotency_key="digest-2026-W32")
    second = master.post_mission("Weekly digest", "summarize", idempotency_key="digest-2026-W32")

    assert first.id == second.id
    assert master.missions(art="summarize").count == 1


def test_master_can_grant_a_realm_but_a_disciple_cannot_take_one(
    master: SectMaster, make_disciple: DiscipleFactory, live_server: str
) -> None:
    make_disciple("ascender")
    assert master.disciple("ascender").realm == "qi-condensation"

    granted = master.grant_realm("ascender", "foundation-establishment")
    assert granted.realm == "foundation-establishment"


def test_stats_counts_the_board(master: SectMaster, make_disciple: DiscipleFactory) -> None:
    mission = master.post_mission("Summarize the week", "summarize")
    master.post_mission("Another", "summarize")
    disciple = make_disciple()
    disciple.claim(mission.id)
    disciple.complete(mission.id, {"ok": True})

    stats = master.stats()

    assert stats.missions.completed == 1
    assert stats.missions.open == 1
    assert stats.by_art["summarize"].completed == 1
    assert stats.disciples.active == 1
