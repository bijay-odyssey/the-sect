"""Exceptions raised by the client SDK.

The server speaks the same ``code`` strings over the wire (see ``ErrorBody``), so
``exception_for_code`` is the single place that maps a response onto a Python type.
"""

from typing import Any


class SectError(Exception):
    """Base for every error the SDK raises."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.detail: dict[str, Any] = detail or {}

    def __str__(self) -> str:
        if self.code:
            return f"[{self.code}] {self.message}"
        return self.message


class SectUnavailable(SectError):
    """The Sect could not be reached, or kept returning 5xx after every retry."""


class AuthError(SectError):
    """Token missing, unrecognised, revoked, or belonging to the wrong principal."""


class NotFound(SectError):
    """No such mission or disciple."""


class MissionNotClaimable(SectError):
    """Someone else holds the mission, or it is no longer open.

    Not an error condition in a polling loop -- it is the expected outcome for every
    disciple but one. Catch it and move to the next mission.
    """


class NotMissionHolder(SectError):
    """The mission is not yours to finish.

    Raised when a lease expired mid-run and the mission was re-claimed, or when it was
    cancelled underneath you. ``detail['reason']`` says which.
    """


class PermanentFailure(SectError):
    """Raise this from a ``run_once`` handler to say "retrying will not help".

    Every other exception a handler raises is reported as retryable, so the mission
    goes back on the board until ``max_attempts`` runs out. A malformed payload or an
    unsupported input should not burn three runs proving that -- raise this instead.
    """


#: Wire ``code`` -> exception class. Anything unlisted falls back by HTTP status.
_CODE_MAP: dict[str, type[SectError]] = {
    "mission_not_claimable": MissionNotClaimable,
    "not_mission_holder": NotMissionHolder,
    "invalid_token": AuthError,
    "missing_token": AuthError,
    "disciple_inactive": AuthError,
    "master_key_required": AuthError,
    "disciple_token_required": AuthError,
    "realm_is_granted": AuthError,
    "forbidden_art": AuthError,
    "mission_forbidden": AuthError,
    "mission_not_found": NotFound,
    "disciple_not_found": NotFound,
}


def exception_for_code(code: str | None, status: int) -> type[SectError]:
    """Pick the exception class for an error response."""
    if code and code in _CODE_MAP:
        return _CODE_MAP[code]
    if status in (401, 403):
        return AuthError
    if status == 404:
        return NotFound
    if status >= 500:
        return SectUnavailable
    return SectError
