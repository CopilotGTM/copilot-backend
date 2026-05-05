"""Regression checks for public lead form rate-limit enforcement and API contracts."""

from types import SimpleNamespace

from fastapi import HTTPException
from src.api.dependencies import rate_limit
from src.api.routers.crm.leads.lead_forms_handler import _public_rate_limit_error_response


class _Request:
    def __init__(self, host: str = "203.0.113.10", forwarded_for: str | None = None) -> None:
        self.client = SimpleNamespace(host=host)
        self.headers = {}
        if forwarded_for:
            self.headers["x-forwarded-for"] = forwarded_for


def test_public_form_read_rate_limit_uses_higher_public_budget(monkeypatch):
    """Read endpoints for public forms should consume the configured higher read bucket."""
    calls = []

    def _record(subject_kind, subject_id, category, *, per_minute_limit, per_hour_limit):
        calls.append((subject_kind, subject_id, category, per_minute_limit, per_hour_limit))

    monkeypatch.setattr(rate_limit, "_enforce_rate_limit_for_limits", _record)

    rate_limit.enforce_public_lead_form_read_rate_limit(_Request(), "lf_test123")

    assert calls == [
        (
            "public_form",
            "lf_test123:203.0.113.10",
            "definition",
            rate_limit.PUBLIC_FORM_READ_PER_MINUTE,
            rate_limit.PUBLIC_FORM_READ_PER_HOUR,
        )
    ]
    assert rate_limit.PUBLIC_FORM_READ_PER_MINUTE > rate_limit.rate_limit_config.READ_PER_MINUTE
    assert rate_limit.PUBLIC_FORM_READ_PER_HOUR > rate_limit.rate_limit_config.READ_PER_HOUR


def test_public_form_submit_rate_limit_uses_higher_public_write_budget(monkeypatch):
    """Submit endpoints for public forms should consume the configured higher write bucket."""
    calls = []
    daily_calls = []

    def _record(subject_kind, subject_id, category, *, per_minute_limit, per_hour_limit):
        calls.append((subject_kind, subject_id, category, per_minute_limit, per_hour_limit))

    def _record_daily(subject_kind, subject_id, category, *, per_day_limit):
        daily_calls.append((subject_kind, subject_id, category, per_day_limit))

    monkeypatch.setattr(rate_limit, "_enforce_rate_limit_for_limits", _record)
    monkeypatch.setattr(rate_limit, "_enforce_daily_rate_limit_for_limit", _record_daily)
    monkeypatch.setattr(rate_limit.rate_limit_config, "TRUST_EXTERNAL_PROXY_HEADERS", True)

    rate_limit.enforce_public_lead_form_submit_rate_limit(
        _Request(forwarded_for="198.51.100.99, 10.0.0.1"),
        "lf_test123",
    )

    assert calls == [
        (
            "public_form",
            "lf_test123:198.51.100.99",
            "submission",
            rate_limit.PUBLIC_FORM_SUBMIT_PER_MINUTE,
            rate_limit.PUBLIC_FORM_SUBMIT_PER_HOUR,
        )
    ]
    assert rate_limit.PUBLIC_FORM_SUBMIT_PER_MINUTE > rate_limit.rate_limit_config.PER_MINUTE
    assert rate_limit.PUBLIC_FORM_SUBMIT_PER_HOUR > rate_limit.rate_limit_config.PER_HOUR
    assert daily_calls == [
        (
            "public_form",
            "lf_test123:198.51.100.99",
            "submission",
            100,
        )
    ]


def test_public_rate_limit_error_uses_public_error_contract():
    """404/429 responses from public rate limit helpers should use stable JSON shape."""
    response = _public_rate_limit_error_response(
        HTTPException(
            status_code=429,
            detail="Too many requests (per-minute limit exceeded)",
            headers={"Retry-After": "42"},
        )
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    assert response.body == (
        b'{"error":{"code":"rate_limited",'
        b'"message":"Too many requests (per-minute limit exceeded)"}}'
    )
