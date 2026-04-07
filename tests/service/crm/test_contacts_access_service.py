"""Tests for ContactsAccessService static/pure helpers."""

from __future__ import annotations

from src.service.crm.access.contacts_access_service import ContactsAccessService


def test_extract_domain_from_email() -> None:
    assert ContactsAccessService._extract_domain("john@acme.com") == "acme.com"
    assert ContactsAccessService._extract_domain("JANE@Example.IO") == "example.io"
    assert ContactsAccessService._extract_domain("bad-email") is None
    assert ContactsAccessService._extract_domain("") is None


def test_split_name() -> None:
    assert ContactsAccessService._split_name("John Doe") == ("John", "Doe")
    assert ContactsAccessService._split_name("Madonna") == ("Madonna", None)
    assert ContactsAccessService._split_name("Mary Jane Watson") == ("Mary", "Jane Watson")
