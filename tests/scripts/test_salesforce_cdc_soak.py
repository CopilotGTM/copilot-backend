import json

import pytest
from scripts.validation.salesforce_cdc_soak import run_soak


@pytest.mark.asyncio
async def test_soak_runs_real_supervisor_and_connectors_in_one_process(tmp_path) -> None:
    output = tmp_path / "salesforce-cdc-soak.json"

    report = await run_soak(customers=3, duration_seconds=2.0, output=output)

    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["customers"] == 3
    assert report["processes"] == 1
    assert report["peak_connectors"] == 3
    assert report["expired_replays"] == 1
    assert report["repairs_published"] == 1
    assert report["healthy_event_spread"] <= 2
    assert report["replay_order_violations"] == 0
    assert report["leaked_sessions"] == 0
    assert report["task_count_high_water"] <= report["task_count_bound"]
    assert report["acceptance_passed"] is True
