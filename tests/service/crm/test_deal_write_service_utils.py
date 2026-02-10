from __future__ import annotations

from datetime import UTC, datetime

from src.service.crm.internal.deal_write_service import DealWriteService


def test_normalize_company_name_compacts_symbols_and_spaces() -> None:
    normalized = DealWriteService._normalize_company_name("  ACME,   INC.  ")
    assert normalized == "acme inc"


def test_normalize_company_name_returns_none_for_empty_input() -> None:
    normalized = DealWriteService._normalize_company_name("   ")
    assert normalized is None


def test_normalize_contact_name_compacts_symbols_and_spaces() -> None:
    normalized = DealWriteService._normalize_contact_name("  Jane,   Doe  ")
    assert normalized == "jane doe"


def test_normalize_email_lowercases_and_trims() -> None:
    normalized = DealWriteService._normalize_email("  Jane.Doe@Acme.COM  ")
    assert normalized == "jane.doe@acme.com"


def test_parse_datetime_normalizes_timezone() -> None:
    parsed = DealWriteService._parse_datetime("2026-02-10T10:30:00")
    assert parsed == "2026-02-10T10:30:00+00:00"


def test_parse_currency_accepts_code_and_separators() -> None:
    parsed = DealWriteService._parse_currency("INR 1,250,000.50")
    assert parsed == 1250000.50


def test_parse_datetime_keeps_existing_timezone() -> None:
    parsed = DealWriteService._parse_datetime(datetime(2026, 2, 10, 10, 30, tzinfo=UTC))
    assert parsed == "2026-02-10T10:30:00+00:00"
