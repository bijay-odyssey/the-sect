"""Registering disciples and tracking who is out there."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from sect.core import sql
from sect.core.auth import (
    AnyPrincipal,
    DisciplePrincipal,
    MasterPrincipal,
    hash_token,
    mint_token,
)
from sect.core.deps import PoolDep
from sect.core.exceptions import SectHTTPError
from sect.core.rows import disciple_from_row
from sect.models import (
    DiscipleCreate,
    DiscipleCreated,
    DiscipleList,
    DisciplePatch,
    DiscipleRecord,
    DiscipleSelfUpdate,
)
from sect.realms import Realm

router = APIRouter(prefix="/disciples", tags=["disciples"])


@router.post("", response_model=DiscipleCreated, status_code=201)
async def register_disciple(
    body: DiscipleCreate,
    principal: MasterPrincipal,
    pool: PoolDep,
) -> DiscipleCreated:
    """Admit a disciple to the Sect and issue its token.

    The token is returned exactly once and only its sha256 is stored, so there is no
    way to recover it later -- rotate instead.
    """
    token = mint_token()
    created = await pool.fetchrow(
        sql.INSERT_DISCIPLE,
        body.name,
        body.display_name,
        body.arts,
        body.repo_url,
        body.description,
        hash_token(token),
    )
    if created is None:
        raise SectHTTPError(
            409,
            "disciple_exists",
            f"A disciple named '{body.name}' is already registered.",
            {"name": body.name},
        )
    record = await pool.fetchrow(sql.SELECT_DISCIPLE, body.name)
    return DiscipleCreated(disciple=disciple_from_row(record), token=token)


@router.get("", response_model=DiscipleList)
async def list_disciples(
    principal: AnyPrincipal,
    pool: PoolDep,
    art: Annotated[str | None, Query()] = None,
    realm: Annotated[Realm | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
) -> DiscipleList:
    rows = await pool.fetch(sql.LIST_DISCIPLES, art, realm, active)
    disciples = [disciple_from_row(row) for row in rows]
    return DiscipleList(disciples=disciples, count=len(disciples))


@router.put("/me", response_model=DiscipleRecord)
async def update_self(
    body: DiscipleSelfUpdate,
    principal: DisciplePrincipal,
    pool: PoolDep,
) -> DiscipleRecord:
    """Announce yourself. This is what the SDK's ``register()`` calls on every wake-up.

    Only the fields actually supplied are written, so a disciple can refresh its
    ``agent_version`` without clearing anything else. ``last_seen_at`` always moves.
    """
    fields = body.model_dump(exclude_unset=True)
    if fields.get("realm") is not None:
        raise SectHTTPError(
            403,
            "realm_is_granted",
            "A disciple does not set its own realm. Ascension is granted by the Sect "
            "via PATCH /v1/disciples/{name} with the master key.",
        )
    fields.pop("realm", None)

    statement, args = sql.build_disciple_update(fields, sql.SELF_UPDATABLE, touch_last_seen=True)
    args[-1] = principal.disciple_id
    await pool.execute(statement, *args)

    record = await pool.fetchrow(sql.SELECT_DISCIPLE, principal.name)
    return disciple_from_row(record)


@router.get("/{name}", response_model=DiscipleRecord)
async def get_disciple(name: str, principal: AnyPrincipal, pool: PoolDep) -> DiscipleRecord:
    row = await pool.fetchrow(sql.SELECT_DISCIPLE, name)
    if row is None:
        raise SectHTTPError(404, "disciple_not_found", f"No disciple named '{name}'.")
    return disciple_from_row(row)


@router.patch("/{name}", response_model=DiscipleRecord)
async def patch_disciple(
    name: str,
    body: DisciplePatch,
    principal: MasterPrincipal,
    pool: PoolDep,
) -> DiscipleRecord:
    """Master-side edits, including ascension to a higher realm."""
    existing = await pool.fetchrow("SELECT id FROM disciples WHERE name = $1", name)
    if existing is None:
        raise SectHTTPError(404, "disciple_not_found", f"No disciple named '{name}'.")

    fields = body.model_dump(exclude_unset=True)
    statement, args = sql.build_disciple_update(fields, sql.MASTER_UPDATABLE, touch_last_seen=False)
    if statement:
        args[-1] = existing["id"]
        await pool.execute(statement, *args)

    record = await pool.fetchrow(sql.SELECT_DISCIPLE, name)
    return disciple_from_row(record)


@router.post("/{name}/token", response_model=DiscipleCreated)
async def rotate_token(
    name: str,
    principal: MasterPrincipal,
    pool: PoolDep,
) -> DiscipleCreated:
    """Issue a fresh token. The previous one stops working immediately."""
    token = mint_token()
    rotated = await pool.fetchrow(sql.ROTATE_TOKEN, name, hash_token(token))
    if rotated is None:
        raise SectHTTPError(404, "disciple_not_found", f"No disciple named '{name}'.")
    record = await pool.fetchrow(sql.SELECT_DISCIPLE, name)
    return DiscipleCreated(disciple=disciple_from_row(record), token=token)
