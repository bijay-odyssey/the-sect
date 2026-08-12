"""asyncpg Record -> pydantic model.

Both models tolerate extra keys, so projections may carry server-side columns
(``claimed_by_id``, ``claim_token``) that must not reach the wire; they are dropped
here by omission rather than by hand.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sect.models import DiscipleRecord, DiscipleStats, Mission


def mission_from_row(row: Mapping[str, Any]) -> Mission:
    return Mission.model_validate(dict(row))


def disciple_from_row(row: Mapping[str, Any]) -> DiscipleRecord:
    data = dict(row)
    data["stats"] = DiscipleStats(
        claimed=data.pop("stat_claimed", 0) or 0,
        completed=data.pop("stat_completed", 0) or 0,
        failed=data.pop("stat_failed", 0) or 0,
    )
    return DiscipleRecord.model_validate(data)
