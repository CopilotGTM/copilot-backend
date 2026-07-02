from __future__ import annotations

from src.service.crm.utils.account import (
    clean_optional_string,
    is_account_linkedin_url,
    is_person_linkedin_url,
    normalize_account_domain,
    normalize_company_name,
)


def test_clean_optional_string_trims_blank_to_none() -> None:
    assert clean_optional_string(None) is None
    assert clean_optional_string("   ") is None
    assert clean_optional_string("  Acme  ") == "Acme"


def test_normalize_account_domain_removes_protocol_www_and_path() -> None:
    assert normalize_account_domain(None) is None
    assert normalize_account_domain("   ") is None
    assert normalize_account_domain("https://www.Acme.com/about?x=1#team") == "acme.com"


def test_normalize_company_name_keeps_exact_matching_key_simple() -> None:
    assert normalize_company_name(None) is None
    assert normalize_company_name("  ") is None
    assert normalize_company_name(" Acme, Inc. ") == "acme inc"


def test_account_linkedin_url_accepts_company_school_and_showcase_pages() -> None:
    """Account LinkedIn identity accepts organization-owned LinkedIn page types."""
    assert is_account_linkedin_url("https://www.linkedin.com/company/acme")
    assert is_account_linkedin_url("linkedin.com/school/stanford-university/")
    assert is_account_linkedin_url("https://in.linkedin.com/showcase/acme-product")


def test_account_linkedin_url_rejects_person_profile_pages() -> None:
    """Account LinkedIn identity rejects personal profile URL paths."""
    assert not is_account_linkedin_url("https://www.linkedin.com/in/jane-buyer")
    assert not is_account_linkedin_url("linkedin.com/pub/jane-buyer/1/2/3")


def test_person_linkedin_url_accepts_profile_pages() -> None:
    """Person LinkedIn identity accepts profile URL paths used by contacts."""
    assert is_person_linkedin_url("https://www.linkedin.com/in/jane-buyer")
    assert is_person_linkedin_url("linkedin.com/pub/jane-buyer/1/2/3")


def test_person_linkedin_url_rejects_account_pages() -> None:
    """Person LinkedIn identity rejects organization page URL paths."""
    assert not is_person_linkedin_url("https://www.linkedin.com/company/acme")
    assert not is_person_linkedin_url("linkedin.com/school/stanford-university")
    assert not is_person_linkedin_url("https://linkedin.com/showcase/acme-product")
