"""Test your peak's handler in isolation -- no Sect, no network.

``handle(mission)`` is a pure function of a :class:`~sect.Mission`. Build one with a
fake payload, call it, and assert on what comes back. This is the whole test surface a
peak needs: the claim/lease/report machinery is the Sect's, and it is already tested
there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from main import ARTS, PEAK, handle

from sect import Mission, PermanentFailure


def make_mission(payload: dict[str, object], *, art: str | None = None) -> Mission:
    now = datetime.now(UTC)
    return Mission(
        id=uuid4(),
        title="a test mission",
        required_art=art or (ARTS[0] if ARTS else "example-art"),
        payload=payload,
        status="claimed",
        not_before=now,
        created_at=now,
        updated_at=now,
    )


def test_config_is_wired() -> None:
    assert PEAK
    assert ARTS, "peak_config.yaml should declare at least one art"


def test_handler_rejects_the_placeholder() -> None:
    """Delete this once you have written a real handler."""
    with pytest.raises(PermanentFailure):
        handle(make_mission({"example": "input"}))


# --- write your real tests here -------------------------------------------- #
#
# def test_handler_summarises() -> None:
#     result = handle(make_mission({"text": "one two three. four five."}))
#     assert result["sentences"] == 2
