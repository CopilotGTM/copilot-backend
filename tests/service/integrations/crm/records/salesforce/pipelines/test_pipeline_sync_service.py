from src.service.integrations.crm.records.salesforce.pipelines.pipeline_sync_service import (
    _select_record_types_for_sync,
)


def test_select_record_types_for_sync_keeps_master_with_custom_record_types() -> None:
    record_types = [
        {"Id": "012MASTER", "IsMaster": True},
        {"Id": "012CUSTOM", "IsMaster": False},
    ]

    selected = _select_record_types_for_sync(record_types)

    assert selected == record_types


def test_select_record_types_for_sync_skips_inactive_record_types() -> None:
    selected = _select_record_types_for_sync(
        [
            {"Id": "012ACTIVE", "IsActive": True},
            {"Id": "012INACTIVE", "IsActive": False},
        ]
    )

    assert selected == [{"Id": "012ACTIVE", "IsActive": True}]
