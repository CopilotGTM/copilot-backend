"""Tests for AccountsAccessService utilities."""

from __future__ import annotations

from src.service.crm.access.accounts_access_service import AccountsAccessService


def test_normalize_domain_plain_uppercase() -> None:
    assert AccountsAccessService._normalize_domain("ACME.COM") == "acme.com"


def test_normalize_domain_https_www() -> None:
    assert AccountsAccessService._normalize_domain("https://www.acme.com") == "acme.com"


def test_normalize_domain_http_with_path() -> None:
    assert AccountsAccessService._normalize_domain("http://Acme.com/about") == "acme.com"


def test_normalize_domain_https_www_with_path() -> None:
    assert AccountsAccessService._normalize_domain("https://www.ACME.com/about") == "acme.com"


def test_normalize_domain_empty_string() -> None:
    assert AccountsAccessService._normalize_domain("") is None


def test_normalize_domain_none() -> None:
    assert AccountsAccessService._normalize_domain(None) is None


def test_normalize_domain_whitespace_only() -> None:
    assert AccountsAccessService._normalize_domain("   ") is None


def test_normalize_domain_strips_query_string() -> None:
    assert AccountsAccessService._normalize_domain("https://acme.com?ref=foo") == "acme.com"


def test_normalize_domain_strips_fragment() -> None:
    assert AccountsAccessService._normalize_domain("https://acme.com#section") == "acme.com"


def test_normalize_domain_no_protocol() -> None:
    assert AccountsAccessService._normalize_domain("www.Acme.COM") == "acme.com"
