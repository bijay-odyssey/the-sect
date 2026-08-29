from types import SimpleNamespace

import pytest

from sect.core.routes.meta import health


class FailingPool:
    async def fetchval(self, query: str) -> None:
        raise ConnectionError("database unavailable")


@pytest.mark.asyncio
async def test_health_logs_database_probe_failure(caplog: pytest.LogCaptureFixture) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FailingPool())))

    with caplog.at_level("WARNING"):
        response = await health(request)

    assert response.status_code == 503
    assert response.body is not None
    assert b'"db":"unreachable"' in response.body
    assert "database probe failed" in caplog.text
    assert "database unavailable" in caplog.text
