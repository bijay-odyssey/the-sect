"""A peak worker, ready to fill in.

A peak is a specialty. This file is one disciple of it: it wakes on a schedule, tells
the Sect it exists (and which peak it serves), takes one mission whose art this peak
covers, does the work, reports the result, and exits. Copy this directory, edit
``peak_config.yaml`` and :func:`handle`, register once, and deploy it anywhere that can
run Python on a timer.

The Sect never sees *how* the work is done. Swap the body of :func:`handle` for an LLM
call, a subprocess, an HTTP request -- nothing else in this file needs to change.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sect import Disciple, Mission, PermanentFailure

CONFIG_PATH = Path(__file__).with_name("peak_config.yaml")


def _load_config(path: Path) -> dict[str, object]:
    """A deliberately tiny YAML reader: ``key: value`` lines, ``#`` comments, and one
    inline ``[a, b]`` list. A peak installs ``the-sect`` and nothing else, so there is
    no PyYAML here -- the same choice the core makes for ``.env`` parsing."""
    data: dict[str, object] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            data[key.strip()] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        else:
            data[key.strip()] = value.strip("\"'")
    return data


CONFIG = _load_config(CONFIG_PATH)
PEAK = str(CONFIG["name"])
ARTS = list(CONFIG.get("arts") or [])
DISCIPLE_NAME = os.environ.get("SECT_DISCIPLE_NAME") or str(
    CONFIG.get("disciple_name") or f"{PEAK}-worker"
)


def handle(mission: Mission) -> object:
    """Do the work. Whatever you return becomes ``mission.result`` (JSON-serialisable).

    Raise anything to report a **retryable** failure: the mission goes back on the board
    and is tried again until ``max_attempts`` runs out. Raise
    :class:`~sect.PermanentFailure` when a retry cannot possibly help -- a malformed
    payload, an unsupported input.
    """
    payload = mission.payload
    # TODO: replace everything below with your peak's actual work.
    raise PermanentFailure(
        f"peak {PEAK!r} has no handler yet -- edit handle() in {Path(__file__).name}. "
        f"(mission wanted art {mission.required_art!r}; payload keys: {sorted(payload)})"
    )


def main() -> int:
    with Disciple(
        name=DISCIPLE_NAME,
        arts=ARTS,
        peak=PEAK,
        display_name=str(CONFIG.get("display_name") or PEAK),
        description=str(CONFIG.get("description") or ""),
        # Free on GitHub Actions, and it tells you which build did the work.
        agent_version=os.environ.get("GITHUB_SHA", "local")[:12],
    ) as disciple:
        mission = disciple.run_once(handle)

    if mission is None:
        print(f"No open missions for peak {PEAK!r} ({', '.join(ARTS)}). Returning to cultivation.")
        return 0

    print(f"{mission.status}: {mission.title}  [{mission.id}]")
    if mission.status == "completed":
        print(json.dumps(mission.result, indent=2, default=str))
        return 0

    print(mission.error or "(no detail)", file=sys.stderr)
    # A retryable failure is ordinary operation -- the mission is back on the board.
    # Only a terminal failure is worth turning the Actions run red.
    return 1 if mission.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
