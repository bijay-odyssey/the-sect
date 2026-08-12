"""The one exception type the routes raise.

Every non-2xx response the Sect emits comes from here or from a handler in
:mod:`sect.core.app`, so the error envelope has exactly one shape.
"""

from __future__ import annotations

from typing import Any


class SectHTTPError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            error["detail"] = self.detail
        return {"error": error}
