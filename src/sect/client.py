"""The client SDK -- the single module a disciple imports.

Depends on ``httpx`` and :mod:`sect.models`, and nothing else. If this file ever grows a
third-party import, something has gone wrong: a disciple repo should be able to
``pip install the-sect`` and get a worker's whole dependency tree.

Two classes, split by principal so a disciple can never accidentally be handed the
master key:

    Disciple    -- register, poll, claim, work, report
    SectMaster  -- post missions, read results, manage disciples
"""

from __future__ import annotations

import os
import random
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from sect.errors import PermanentFailure, SectError, SectUnavailable, exception_for_code
from sect.models import (
    ClaimResponse,
    DiscipleRecord,
    HeartbeatResponse,
    Mission,
    MissionList,
    SectStats,
)
from sect.realms import Realm

#: The host may be waking from idle. On a free-tier platform that wait shows up as a
#: slow *response* -- the edge accepts the connection immediately and holds it while the
#: container boots -- so the read timeout is the one that has to be generous.
DEFAULT_TIMEOUT = 90.0
DEFAULT_CONNECT_TIMEOUT = 15.0

DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_CAP = 20.0

#: Transient by definition: the request never reached a handler, or the host is telling
#: us to come back. Everything else in the 4xx range is deterministic and retrying it
#: just wastes a runner's minutes.
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)

#: missions.error is capped server-side; truncate rather than getting a 422 on top of
#: whatever already went wrong.
_MAX_ERROR_CHARS = 8_000


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _iso(value: datetime | str | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


class _Client:
    """Auth, retries, and error translation. Shared by both principals."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            follow_redirects=True,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            is_last = attempt == self._max_retries
            try:
                response = self._http.request(method, path, json=json, params=params)
            except _RETRY_EXCEPTIONS as exc:
                if is_last:
                    raise SectUnavailable(
                        f"No answer from the Sect at {self._http.base_url} after "
                        f"{self._max_retries + 1} attempts: {exc!r}"
                    ) from exc
                self._wait(attempt, None)
                continue

            if response.status_code in _RETRY_STATUSES and not is_last:
                self._wait(attempt, response.headers.get("retry-after"))
                continue
            if response.status_code >= 400:
                raise self._to_error(response)
            return response

        raise AssertionError("unreachable")  # pragma: no cover

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), self._backoff_cap))
                return
            except ValueError:
                pass  # HTTP-date form; fall through to ordinary backoff
        # Full jitter: spreads a herd of cron disciples that all woke on the same minute.
        ceiling = min(self._backoff_cap, self._backoff_base * (2**attempt))
        time.sleep(random.uniform(0, ceiling))

    @staticmethod
    def _to_error(response: httpx.Response) -> SectError:
        code: str | None = None
        message: str | None = None
        detail: dict[str, Any] | None = None
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                error = body["error"]
                code = error.get("code")
                message = error.get("message")
                detail = error.get("detail")
        except ValueError:
            pass

        message = message or f"HTTP {response.status_code} from {response.request.url}"
        return exception_for_code(code, response.status_code)(
            message, code=code, status=response.status_code, detail=detail
        )

    def close(self) -> None:
        self._http.close()


def _resolve(explicit: str | None, env_var: str, what: str) -> str:
    value = explicit or os.environ.get(env_var)
    if not value:
        raise SectError(f"No {what}: pass it explicitly or set {env_var}.")
    return value


class Disciple:
    """A worker's handle on the Sect.

    The reference deployment is a scheduled job that wakes, does one mission and exits::

        d = Disciple(name="scribe", arts=["summarize"])
        d.run_once(handle)

    ``base_url`` and ``token`` default to ``$SECT_URL`` and ``$SECT_TOKEN``.
    """

    def __init__(
        self,
        name: str,
        arts: Sequence[str],
        *,
        base_url: str | None = None,
        token: str | None = None,
        display_name: str | None = None,
        repo_url: str | None = None,
        description: str | None = None,
        agent_version: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = name
        self.arts = list(arts)
        self.display_name = display_name
        self.repo_url = repo_url
        self.description = description
        self.agent_version = agent_version

        self._client = _Client(
            _resolve(base_url, "SECT_URL", "base URL"),
            _resolve(token, "SECT_TOKEN", "token"),
            timeout=timeout,
            connect_timeout=connect_timeout,
            max_retries=max_retries,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
            transport=transport,
        )
        # A claim token only exists in the process that won the claim. Keeping it here
        # is what lets complete()/fail()/heartbeat() take the signature they do.
        self._claim_tokens: dict[UUID, UUID] = {}

    # --- lifecycle -------------------------------------------------------- #

    def register(self) -> DiscipleRecord:
        """Announce yourself and refresh ``last_seen_at``.

        Safe to call on every wake-up. Only the fields this Disciple was constructed
        with are sent, so nothing you did not mention gets cleared.
        """
        body: dict[str, Any] = {"arts": self.arts}
        for field, value in (
            ("display_name", self.display_name),
            ("repo_url", self.repo_url),
            ("description", self.description),
            ("agent_version", self.agent_version),
        ):
            if value is not None:
                body[field] = value
        response = self._client.request("PUT", "/v1/disciples/me", json=body)
        return DiscipleRecord.model_validate(response.json())

    # --- the board -------------------------------------------------------- #

    def poll_missions(
        self, art: str | Iterable[str] | None = None, limit: int = 20
    ) -> list[Mission]:
        """Claimable missions, best first. Defaults to this disciple's own arts."""
        params: dict[str, Any] = {"limit": limit}
        if art is not None:
            params["art"] = [art] if isinstance(art, str) else list(art)
        response = self._client.request("GET", "/v1/missions/open", params=params)
        return [Mission.model_validate(item) for item in response.json()["missions"]]

    def claim(self, mission_id: UUID | str, lease_seconds: int | None = None) -> Mission:
        """Claim one specific mission, or raise :class:`MissionNotClaimable`.

        Safe to retry: the server treats a re-claim by the same disciple as idempotent,
        so a lost response costs nothing.
        """
        response = self._client.request(
            "POST",
            f"/v1/missions/{_as_uuid(mission_id)}/claim",
            json={"lease_seconds": lease_seconds},
        )
        return self._remember(response)

    def claim_next(
        self,
        art: str | Iterable[str] | None = None,
        lease_seconds: int | None = None,
    ) -> Mission | None:
        """Atomically take the best matching mission, or ``None`` if there is none."""
        body: dict[str, Any] = {"lease_seconds": lease_seconds}
        if art is not None:
            body["arts"] = [art] if isinstance(art, str) else list(art)
        response = self._client.request("POST", "/v1/missions/claim-next", json=body)
        if response.status_code == 204:
            return None
        return self._remember(response)

    def _remember(self, response: httpx.Response) -> Mission:
        claim = ClaimResponse.model_validate(response.json())
        self._claim_tokens[claim.mission.id] = claim.claim_token
        return claim.mission

    def _token_for(self, mission_id: UUID) -> UUID:
        try:
            return self._claim_tokens[mission_id]
        except KeyError:
            raise SectError(
                f"This disciple holds no claim token for mission {mission_id}. "
                "Claim it first -- a claim token exists only in the process that won "
                "the claim, and does not survive a restart."
            ) from None

    # --- reporting -------------------------------------------------------- #

    def complete(self, mission_id: UUID | str, result: Any = None) -> Mission:
        """Report success. Safe to retry: an exact replay returns the same mission."""
        mission_id = _as_uuid(mission_id)
        response = self._client.request(
            "POST",
            f"/v1/missions/{mission_id}/complete",
            json={"claim_token": str(self._token_for(mission_id)), "result": result},
        )
        return Mission.model_validate(response.json())

    def fail(
        self,
        mission_id: UUID | str,
        error: str,
        *,
        retryable: bool = True,
        retry_after_seconds: int | None = None,
    ) -> Mission:
        """Report failure. Retryable failures go back on the board after a backoff."""
        mission_id = _as_uuid(mission_id)
        response = self._client.request(
            "POST",
            f"/v1/missions/{mission_id}/fail",
            json={
                "claim_token": str(self._token_for(mission_id)),
                "error": error[:_MAX_ERROR_CHARS],
                "retryable": retryable,
                "retry_after_seconds": retry_after_seconds,
            },
        )
        return Mission.model_validate(response.json())

    def heartbeat(self, mission_id: UUID | str, extend_seconds: int | None = None) -> datetime:
        """Push the lease out on a mission still being worked."""
        mission_id = _as_uuid(mission_id)
        response = self._client.request(
            "POST",
            f"/v1/missions/{mission_id}/heartbeat",
            json={
                "claim_token": str(self._token_for(mission_id)),
                "extend_seconds": extend_seconds,
            },
        )
        return HeartbeatResponse.model_validate(response.json()).lease_expires_at

    # --- the whole job, in one call --------------------------------------- #

    def run_once(
        self,
        handler: Callable[[Mission], Any],
        *,
        art: str | Iterable[str] | None = None,
        lease_seconds: int | None = None,
        register: bool = True,
    ) -> Mission | None:
        """Claim one mission, run it, report the outcome. Returns ``None`` if idle.

        This is the entire body of a scheduled disciple. Whatever ``handler`` returns
        becomes the mission result; whatever it raises becomes a retryable failure,
        unless it raises :class:`~sect.errors.PermanentFailure`.
        """
        if register:
            self.register()

        mission = self.claim_next(art=art, lease_seconds=lease_seconds)
        if mission is None:
            return None

        try:
            result = handler(mission)
        except PermanentFailure as exc:
            return self.fail(mission.id, str(exc), retryable=False)
        except Exception as exc:
            report = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
            return self.fail(mission.id, report, retryable=True)
        return self.complete(mission.id, result)

    # --- housekeeping ------------------------------------------------------ #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Disciple:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SectMaster:
    """The master key's view: post work, read results, manage disciples.

    ``base_url`` and ``master_key`` default to ``$SECT_URL`` and ``$SECT_MASTER_KEY``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        master_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = _Client(
            _resolve(base_url, "SECT_URL", "base URL"),
            _resolve(master_key, "SECT_MASTER_KEY", "master key"),
            timeout=timeout,
            connect_timeout=connect_timeout,
            max_retries=max_retries,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
            transport=transport,
        )

    # --- disciples --------------------------------------------------------- #

    def register_disciple(
        self,
        name: str,
        arts: Sequence[str],
        *,
        display_name: str | None = None,
        repo_url: str | None = None,
        description: str | None = None,
    ) -> tuple[DiscipleRecord, str]:
        """Admit a disciple. Returns the record and its token -- shown only this once."""
        response = self._client.request(
            "POST",
            "/v1/disciples",
            json={
                "name": name,
                "arts": list(arts),
                "display_name": display_name,
                "repo_url": repo_url,
                "description": description,
            },
        )
        body = response.json()
        return DiscipleRecord.model_validate(body["disciple"]), body["token"]

    def disciples(
        self,
        *,
        art: str | None = None,
        realm: Realm | None = None,
        active: bool | None = None,
    ) -> list[DiscipleRecord]:
        candidates = (("art", art), ("realm", realm), ("active", active))
        params = {key: value for key, value in candidates if value is not None}
        response = self._client.request("GET", "/v1/disciples", params=params)
        return [DiscipleRecord.model_validate(d) for d in response.json()["disciples"]]

    def disciple(self, name: str) -> DiscipleRecord:
        response = self._client.request("GET", f"/v1/disciples/{name}")
        return DiscipleRecord.model_validate(response.json())

    def grant_realm(self, name: str, realm: Realm) -> DiscipleRecord:
        """Elevate a disciple. Only the Sect may do this; a disciple cannot promote
        itself."""
        response = self._client.request("PATCH", f"/v1/disciples/{name}", json={"realm": realm})
        return DiscipleRecord.model_validate(response.json())

    def set_active(self, name: str, active: bool) -> DiscipleRecord:
        response = self._client.request("PATCH", f"/v1/disciples/{name}", json={"active": active})
        return DiscipleRecord.model_validate(response.json())

    def rotate_token(self, name: str) -> str:
        """Issue a fresh token. The old one stops working immediately."""
        response = self._client.request("POST", f"/v1/disciples/{name}/token")
        return response.json()["token"]

    # --- missions ---------------------------------------------------------- #

    def post_mission(
        self,
        title: str,
        required_art: str,
        *,
        payload: dict[str, Any] | None = None,
        description: str | None = None,
        priority: int = 0,
        lease_seconds: int | None = None,
        max_attempts: int | None = None,
        not_before: datetime | str | None = None,
        idempotency_key: str | None = None,
    ) -> Mission:
        response = self._client.request(
            "POST",
            "/v1/missions",
            json={
                "title": title,
                "required_art": required_art,
                "payload": payload or {},
                "description": description,
                "priority": priority,
                "lease_seconds": lease_seconds,
                "max_attempts": max_attempts,
                "not_before": _iso(not_before),
                "idempotency_key": idempotency_key,
            },
        )
        return Mission.model_validate(response.json())

    def mission(self, mission_id: UUID | str) -> Mission:
        response = self._client.request("GET", f"/v1/missions/{_as_uuid(mission_id)}")
        return Mission.model_validate(response.json())

    def missions(
        self,
        *,
        status: str | None = None,
        art: str | None = None,
        claimed_by: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> MissionList:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("status", status),
            ("art", art),
            ("claimed_by", claimed_by),
            ("cursor", cursor),
        ):
            if value is not None:
                params[key] = value
        response = self._client.request("GET", "/v1/missions", params=params)
        return MissionList.model_validate(response.json())

    def cancel(self, mission_id: UUID | str) -> Mission:
        response = self._client.request("POST", f"/v1/missions/{_as_uuid(mission_id)}/cancel")
        return Mission.model_validate(response.json())

    # --- operations -------------------------------------------------------- #

    def stats(self) -> SectStats:
        response = self._client.request("GET", "/v1/stats")
        return SectStats.model_validate(response.json())

    def sweep(self) -> int:
        """Mark zombies failed on demand. Returns how many were swept."""
        response = self._client.request("POST", "/v1/admin/sweep")
        return int(response.json()["swept"])

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SectMaster:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
