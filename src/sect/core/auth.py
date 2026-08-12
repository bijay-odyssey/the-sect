"""Who is calling, and what that entitles them to.

Two principals. The master key is a single env-var secret compared in constant time;
a disciple token is looked up by sha256 hash, so the database never stores anything
replayable.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Request

from sect.core import sql
from sect.core.exceptions import SectHTTPError

#: Prefix on every issued disciple token. Makes them recognisable to secret scanners
#: and to you in a log line.
TOKEN_PREFIX = "sect_d_"


def mint_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Principal:
    kind: Literal["master", "disciple"]
    name: str
    disciple_id: UUID | None = None
    arts: tuple[str, ...] = field(default=())

    @property
    def is_master(self) -> bool:
        return self.kind == "master"


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header:
        raise SectHTTPError(401, "missing_token", "Authorization header is required.")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise SectHTTPError(401, "missing_token", "Expected 'Authorization: Bearer <token>'.")
    return token


async def current_principal(request: Request) -> Principal:
    token = _bearer_token(request)
    settings = request.app.state.settings

    if hmac.compare_digest(token, settings.master_key):
        return Principal(kind="master", name="master")

    row = await request.app.state.pool.fetchrow(
        sql.SELECT_DISCIPLE_BY_TOKEN_HASH, hash_token(token)
    )
    if row is None:
        raise SectHTTPError(401, "invalid_token", "Unrecognised token.")
    if not row["active"]:
        raise SectHTTPError(
            401,
            "disciple_inactive",
            f"Disciple '{row['name']}' has been deactivated.",
            {"disciple": row["name"]},
        )
    return Principal(
        kind="disciple",
        name=row["name"],
        disciple_id=row["id"],
        arts=tuple(row["arts"]),
    )


async def require_master(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_master:
        raise SectHTTPError(403, "master_key_required", "This endpoint requires the master key.")
    return principal


async def require_disciple(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if principal.kind != "disciple":
        raise SectHTTPError(
            403,
            "disciple_token_required",
            "This endpoint acts on behalf of a disciple, so it requires a disciple token. "
            "The master key has no identity to claim or complete missions with.",
        )
    return principal


AnyPrincipal = Annotated[Principal, Depends(current_principal)]
MasterPrincipal = Annotated[Principal, Depends(require_master)]
DisciplePrincipal = Annotated[Principal, Depends(require_disciple)]
