from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import Request

from sect.core.app import create_app


@pytest.mark.asyncio
async def test_unhandled_exception_uses_json_envelope_and_is_logged(settings, caplog):
    app = create_app(settings=settings, pool=None)
    logging.getLogger().addHandler(caplog.handler)

    @app.get("/test-unhandled-exception")
    async def raise_unhandled_exception(request: Request) -> None:
        raise RuntimeError("secret implementation detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://sect.test") as client:
        with caplog.at_level("ERROR", logger="sect.core"):
            response = await client.get("/test-unhandled-exception")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An internal server error occurred.",
        }
    }
    assert "secret implementation detail" not in response.text
    assert "Unhandled application exception" in caplog.text
    assert "secret implementation detail" in caplog.text
