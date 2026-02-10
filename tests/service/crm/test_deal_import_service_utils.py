from __future__ import annotations

from src.service.crm.internal.deal_import_service import DealImportService


def test_infer_scalar_type_detects_datetime() -> None:
    inferred = DealImportService._infer_scalar_type(
        ["2026-02-10T09:15:00Z", "2026-02-11 18:30:45+05:30"]
    )
    assert inferred == "datetime"


def test_infer_scalar_type_detects_currency() -> None:
    inferred = DealImportService._infer_scalar_type(["INR 1,200,000", "$2500.75"])
    assert inferred == "currency"


def test_infer_scalar_type_keeps_plain_numbers_as_number() -> None:
    inferred = DealImportService._infer_scalar_type(["1200000", "2500.75"])
    assert inferred == "number"
