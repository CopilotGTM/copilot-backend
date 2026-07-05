from scripts.security import validate_health_security as validator


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_public_health_passes_without_sensitive_terms(monkeypatch):
    def fake_get(url, timeout):
        return FakeResponse(200, {"status": "healthy", "service": "api"})

    monkeypatch.setattr(validator.requests, "get", fake_get)

    result = validator.test_public_health_endpoint("https://api.example.com", 1)

    assert result["passed"] is True
    assert result["sensitive_terms"] == []


def test_public_health_flags_sensitive_terms(monkeypatch):
    def fake_get(url, timeout):
        return FakeResponse(200, {"status": "healthy", "database": "connected"})

    monkeypatch.setattr(validator.requests, "get", fake_get)

    result = validator.test_public_health_endpoint("https://api.example.com", 1)

    assert result["passed"] is False
    assert result["sensitive_terms"] == ["database"]


def test_internal_health_accepts_401_or_403_as_restricted(monkeypatch):
    responses = iter([FakeResponse(403), FakeResponse(401), FakeResponse(403)])

    def fake_get(url, timeout):
        return next(responses)

    monkeypatch.setattr(validator.requests, "get", fake_get)

    results = validator.test_internal_health_endpoints("https://api.example.com", 1)

    assert all(result["passed"] for result in results)
    assert [result["status_code"] for result in results] == [403, 401, 403]


def test_text_report_contains_overall_status():
    report = {
        "base_url": "https://api.example.com",
        "passed": True,
        "public_health": {
            "endpoint": "/health",
            "status_code": 200,
            "accessible": True,
            "sensitive_terms": [],
            "missing_security_headers": ["x-frame-options"],
        },
        "internal_health": [
            {
                "endpoint": "/internal/healthcheck",
                "status_code": 403,
                "properly_restricted": True,
                "passed": True,
            }
        ],
    }

    text = validator.format_text_report(report)

    assert "Overall: PASS" in text
    assert "missing_security_headers: x-frame-options" in text
