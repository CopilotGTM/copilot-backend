from __future__ import annotations

from src.service.crm.utils.identity import extract_email_domain, normalize_email


def test_normalize_email_trims_and_lowercases() -> None:
    assert normalize_email("  Buyer@Example.COM  ") == "buyer@example.com"


def test_normalize_email_returns_none_for_empty_values() -> None:
    assert normalize_email(None) is None
    assert normalize_email("   ") is None


def test_extract_email_domain_returns_lowercase_domain() -> None:
    assert extract_email_domain(" Buyer@Example.COM ") == "example.com"


def test_extract_email_domain_returns_none_without_domain() -> None:
    assert extract_email_domain("bad-email") is None
    assert extract_email_domain("buyer@") is None
    assert extract_email_domain(None) is None
