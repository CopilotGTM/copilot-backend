from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.api.schemas.crm_deal_import_models import (
    DealImportCustomColumnType,
    DealImportProcessRequest,
    DealImportUploadRequest,
)


def test_upload_request_rejects_non_csv_filename() -> None:
    with pytest.raises(ValidationError):
        DealImportUploadRequest(filename="deals.xlsx")


def test_upload_request_accepts_csv_filename() -> None:
    request = DealImportUploadRequest(filename="deals.csv")
    assert request.filename == "deals.csv"


def test_process_request_rejects_non_csv_file_type() -> None:
    with pytest.raises(ValidationError):
        DealImportProcessRequest(storage_path="crm_imports/deals/u/f.csv", file_type="xlsx")


def test_process_request_normalizes_custom_column_type_keys() -> None:
    request = DealImportProcessRequest(
        storage_path="crm_imports/deals/u/f.csv",
        custom_column_types={
            "custom_Steel Target": DealImportCustomColumnType.INTEGER,
            "custom our_revenue": "number",
        },
    )

    assert request.custom_column_types == {
        "steel_target": DealImportCustomColumnType.INTEGER,
        "our_revenue": DealImportCustomColumnType.NUMBER,
    }


def test_process_request_rejects_non_object_custom_column_types() -> None:
    with pytest.raises(ValidationError):
        DealImportProcessRequest(
            storage_path="crm_imports/deals/u/f.csv",
            custom_column_types=["custom_scope:text"],
        )


def test_process_request_accepts_datetime_and_currency_custom_types() -> None:
    request = DealImportProcessRequest(
        storage_path="crm_imports/deals/u/f.csv",
        custom_column_types={
            "custom_contract_signed_at": "datetime",
            "custom_project_budget": "currency",
        },
    )

    assert request.custom_column_types == {
        "contract_signed_at": DealImportCustomColumnType.DATETIME,
        "project_budget": DealImportCustomColumnType.CURRENCY,
    }
