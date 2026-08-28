"""Structured logging: one JSON object per line, stdlib only.

Wired up from :func:`sect.core.app.create_app`. Emits an access line per HTTP request
carrying the method, path, status, duration, and -- where the request had them -- the
calling disciple and the mission id from the path. Anything the app logs itself with
``extra={...}`` is merged into the same JSON object.

No dependency: ``json`` plus the ``logging`` module. Set ``SECT_LOG_JSON=false`` to fall
back to plain text (the test suite does, to keep output readable).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

# Attributes every LogRecord carries. Anything else on a record was passed via `extra=`
# and belongs in the serialized output.
_STANDARD = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}

_MISSION_ID = re.compile(r"/missions/([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})")


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str, *, json_lines: bool) -> None:
    """Install a single stderr handler on the root logger. Idempotent."""
    handler = logging.StreamHandler()
    if json_lines:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


async def access_log_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Log one line per request. ``request.state.principal`` is set by
    :func:`sect.core.auth.current_principal` when the endpoint authenticates."""
    log = logging.getLogger("sect.core.access")
    start = time.perf_counter()
    response = await call_next(request)
    principal = getattr(request.state, "principal", None)
    match = _MISSION_ID.search(request.url.path)
    log.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            "disciple": getattr(principal, "name", None),
            "mission_id": match.group(1) if match else None,
        },
    )
    return response
