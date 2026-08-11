import inspect

from scripts.maintenance.enable_salesforce_aggregate_cdc import (
    build_aggregate_metadata,
    enable_aggregate_cdc,
)


def test_aggregate_migration_defaults_to_dry_run() -> None:
    assert inspect.signature(enable_aggregate_cdc).parameters["apply"].default is False


def test_aggregate_migration_starts_earliest_without_copying_legacy_cursor() -> None:
    before = {
        "initial_sync_status": "success",
        "salesforce_cdc": {
            "opportunity": {"last_replay_id": "legacy-opportunity"},
            "account": {"last_replay_id": "legacy-account"},
        },
    }

    after = build_aggregate_metadata(before)

    assert after["salesforce_cdc"]["mode"] == "aggregate"
    assert after["salesforce_cdc"]["aggregate"]["last_replay_id"] is None
    assert after["salesforce_cdc"]["aggregate"]["replay_preset"] == "EARLIEST"
    assert after["salesforce_cdc"]["opportunity"] == before["salesforce_cdc"]["opportunity"]
    assert before["salesforce_cdc"].get("mode") is None
