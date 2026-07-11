"""Contracts for the deterministic, non-production WhatsApp fault gate."""

from __future__ import annotations

import pytest
from scripts.load_testing.whatsapp_durable_delivery import (
    SimulationConfig,
    build_report,
    run_simulation,
)


def test_dry_run_validates_without_generating_jobs() -> None:
    report = build_report(SimulationConfig(messages=100), apply=False)

    assert report["mode"] == "dry-run"
    assert report["configuration_valid"] is True
    assert report["jobs_generated"] == 0
    assert report["apply_required"] is True


def test_fault_model_converges_without_silent_loss() -> None:
    config = SimulationConfig(
        messages=100,
        duplicate_replay=True,
        fail_after_db_commit=7,
        fail_enqueue=11,
        kill_workers=9,
        fail_meta_transient=13,
        workers=2,
        scaled_workers=5,
    )

    report = run_simulation(config)

    assert report["passed"] is True
    assert report["unique_jobs"] == 100
    assert report["jobs_after_duplicate_replay"] == 100
    assert report["complete_dispatch_envelopes"] == 100
    assert report["recovered_post_commit_failures"] == 7
    assert report["recovered_enqueue_failures"] == 11
    assert report["worker_losses_injected"] == 9
    assert report["meta_transient_failures_recovered"] == 13
    assert report["stale_pending_jobs"] == 0
    assert report["delivery_outcomes"] == 100
    assert report["chunks_without_provider_id"] == 0
    assert report["terminal_failures_without_evidence"] == 0
    assert (
        report["oldest_queue_age_after_scale_seconds"]
        < report["oldest_queue_age_before_scale_seconds"]
    )
    assert report["reconstructable_by_modality"] == {
        "audio": 25,
        "document": 25,
        "image": 25,
        "text": 25,
    }


def test_fault_counts_cannot_exceed_message_count() -> None:
    with pytest.raises(ValueError, match="fail_enqueue"):
        run_simulation(SimulationConfig(messages=10, fail_enqueue=11))

    with pytest.raises(ValueError, match="plus fail_enqueue"):
        run_simulation(
            SimulationConfig(
                messages=10,
                fail_after_db_commit=6,
                fail_enqueue=5,
            )
        )
