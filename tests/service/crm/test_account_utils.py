from __future__ import annotations

from src.service.crm.utils.account import (
    clean_optional_string,
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
