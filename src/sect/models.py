"""The wire contract, imported by both sides.

This module is the single source of truth for every request and response shape. The
server validates against it and the SDK parses against it, so the two cannot drift.

Nothing here may import from :mod:`sect.core`: a disciple installs the base
distribution and has neither FastAPI nor asyncpg available.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sect.realms import Realm

# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

#: A disciple's public identifier. Lowercase slug, 1-64 chars. Matches the CHECK
#: constraint on ``disciples.name`` -- keep the two in step.
DiscipleName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")]

#: A skill tag. Same shape as a name, but dots and underscores are allowed so tags can
#: be namespaced (``docs.summarize``) without inventing a hierarchy in v0.1.
Art = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$")]

MissionStatus = Literal["open", "claimed", "completed", "failed", "cancelled"]

#: Statuses from which there is no way back.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

MIN_LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 86_400


# --------------------------------------------------------------------------- #
# Missions
# --------------------------------------------------------------------------- #


class Mission(BaseModel):
    """A mission as it appears on the wire.

    ``claim_token`` is deliberately absent. It is a per-claim secret handed only to the
    disciple that won the claim, and it travels in :class:`ClaimResponse` alone -- if it
    appeared here, any disciple could read another's token off ``GET /v1/missions/{id}``
    and steal its completion.
    """

    model_config = ConfigDict(extra="ignore")

    id: UUID
    title: str
    description: str | None = None
    required_art: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    status: MissionStatus

    attempts: int = 0
    max_attempts: int = 3
    lease_seconds: int = 900
    not_before: datetime

    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None

    result: Any = None
    error: str | None = None

    idempotency_key: str | None = None
    posted_by: str = "master"
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _mask_stale_lease(self) -> Mission:
        """A lease only means something while the mission is held.

        The database keeps holder columns after a mission finishes so that a retried
        ``complete()`` can be recognised as a replay rather than a conflict. Of that
        residue only ``lease_expires_at`` is misleading on the wire -- ``claimed_by`` and
        ``claimed_at`` are genuine history worth reading off a finished board.
        """
        if self.status != "claimed":
            self.lease_expires_at = None
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class MissionCreate(BaseModel):
    """Body of ``POST /v1/missions``. Only title and required_art are mandatory."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    required_art: Art
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-32_768, le=32_767)
    lease_seconds: int | None = Field(default=None, ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)
    max_attempts: int | None = Field(default=None, ge=1, le=100)
    not_before: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_seconds: int | None = Field(default=None, ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)


class ClaimNextRequest(ClaimRequest):
    #: Defaults to the calling disciple's registered arts.
    arts: list[Art] | None = Field(default=None, min_length=1)


class ClaimResponse(BaseModel):
    """The only place ``claim_token`` is ever disclosed."""

    mission: Mission
    claim_token: UUID


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: UUID
    extend_seconds: int | None = Field(default=None, ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)


class HeartbeatResponse(BaseModel):
    lease_expires_at: datetime


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: UUID
    #: Arbitrary JSON. The Sect never inspects it.
    result: Any = None


class FailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: UUID
    error: str = Field(min_length=1, max_length=8_000)
    #: False means "do not try this again" regardless of attempts remaining.
    retryable: bool = True
    retry_after_seconds: int | None = Field(default=None, ge=0, le=MAX_LEASE_SECONDS)


# --------------------------------------------------------------------------- #
# Disciples
# --------------------------------------------------------------------------- #


class DiscipleStats(BaseModel):
    """Mission counts attributed to a disciple.

    ``failed`` counts terminal failures only. A retryable failure clears the holder
    columns and returns the mission to the board, so it is attributed to nobody.
    """

    claimed: int = 0
    completed: int = 0
    failed: int = 0


class DiscipleRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    display_name: str | None = None
    arts: list[str]
    realm: Realm
    repo_url: str | None = None
    description: str | None = None
    agent_version: str | None = None
    active: bool = True
    last_seen_at: datetime | None = None
    created_at: datetime
    stats: DiscipleStats = Field(default_factory=DiscipleStats)


class DiscipleCreate(BaseModel):
    """Body of ``POST /v1/disciples``. Master key only.

    ``realm`` is absent by design: every disciple starts at the bottom of the ladder.
    """

    model_config = ConfigDict(extra="forbid")

    name: DiscipleName
    display_name: str | None = None
    arts: list[Art] = Field(min_length=1)
    repo_url: str | None = None
    description: str | None = None


class DiscipleCreated(BaseModel):
    """Registration response. The token is shown exactly once and never again."""

    disciple: DiscipleRecord
    token: str


class DiscipleSelfUpdate(BaseModel):
    """Body of ``PUT /v1/disciples/me`` -- what a disciple may say about itself.

    Fields left unset are not touched, so a disciple can refresh only its
    ``agent_version`` without clearing its description.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    arts: list[Art] | None = Field(default=None, min_length=1)
    repo_url: str | None = None
    description: str | None = None
    agent_version: str | None = Field(default=None, max_length=200)

    #: Declared only so the route can answer a specific 403 realm_is_granted instead of
    #: a bare 422. A disciple does not promote itself; the Sect elevates it.
    realm: str | None = None


class DisciplePatch(BaseModel):
    """Body of ``PATCH /v1/disciples/{name}``. Master key only. This is where ascension
    happens."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    arts: list[Art] | None = Field(default=None, min_length=1)
    repo_url: str | None = None
    description: str | None = None
    realm: Realm | None = None
    active: bool | None = None


# --------------------------------------------------------------------------- #
# Collections, stats, errors
# --------------------------------------------------------------------------- #


class OpenMissionList(BaseModel):
    missions: list[Mission]
    count: int


class MissionList(BaseModel):
    missions: list[Mission]
    count: int
    #: Opaque keyset cursor. Pass back as ``?cursor=`` for the next page.
    next_cursor: str | None = None


class DiscipleList(BaseModel):
    disciples: list[DiscipleRecord]
    count: int


class MissionCounts(BaseModel):
    open: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0


class DiscipleCounts(BaseModel):
    total: int = 0
    active: int = 0


class SectStats(BaseModel):
    missions: MissionCounts
    by_art: dict[str, MissionCounts]
    disciples: DiscipleCounts


class SweepResult(BaseModel):
    swept: int


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "unreachable"]
    version: str
    time: datetime


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """Every non-2xx response from sect-core has this shape."""

    error: ErrorBody
