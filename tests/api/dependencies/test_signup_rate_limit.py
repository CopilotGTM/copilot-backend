from collections import defaultdict

import pytest
from fastapi import HTTPException
from src.api.dependencies import rate_limit as module
from starlette.requests import Request


class _Redis:
    def __init__(self):
        self.counts = defaultdict(int)

    def incr(self, key):
        self.counts[key] += 1
        return self.counts[key]

    def expire(self, _key, _seconds):
        return True

    def ttl(self, _key):
        return 60


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/users/auth/signup-requests/request-id",
            "headers": [(b"authorization", b"Bearer stable-session-token")],
            "client": ("203.0.113.10", 1234),
        }
    )


@pytest.mark.asyncio
async def test_signup_status_polling_has_a_separate_read_budget(monkeypatch) -> None:
    redis = _Redis()
    monkeypatch.setattr(module, "get_redis_client", lambda: redis)
    monkeypatch.setattr(
        module.rate_limit_config, "AUTH_SIGNUP_STATUS_PER_MINUTE", 60, raising=False
    )
    monkeypatch.setattr(module.rate_limit_config, "AUTH_SIGNUP_STATUS_PER_HOUR", 600, raising=False)

    for _ in range(10):
        await module.rate_limit_signup_status(_request())


@pytest.mark.asyncio
async def test_signup_status_read_budget_still_throttles_abuse(monkeypatch) -> None:
    redis = _Redis()
    monkeypatch.setattr(module, "get_redis_client", lambda: redis)
    monkeypatch.setattr(module.rate_limit_config, "AUTH_SIGNUP_STATUS_PER_MINUTE", 2, raising=False)
    monkeypatch.setattr(module.rate_limit_config, "AUTH_SIGNUP_STATUS_PER_HOUR", 10, raising=False)

    await module.rate_limit_signup_status(_request())
    await module.rate_limit_signup_status(_request())
    with pytest.raises(HTTPException) as error:
        await module.rate_limit_signup_status(_request())

    assert error.value.status_code == 429
