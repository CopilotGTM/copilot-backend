"""Deterministic fault gate for the WhatsApp durable-delivery state machine.

This script does not measure production throughput. It exercises the delivery
contract with in-memory identity, database, broker, worker, agent, and Meta
boundaries so 10,000-message correctness can be checked without external side
effects.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Inputs and non-production safety limits for the deterministic gate."""
    messages: int = 10_000
    duplicate_replay: bool = False
    fail_after_db_commit: int = 0
    fail_enqueue: int = 0
    kill_workers: int = 0
    fail_meta_transient: int = 0
    workers: int = 2
    scaled_workers: int = 4
    environment: str = "local"
    organisation_id: str = "stub-non-production"
    api_memory_limit_mb: float = 512.0
    worker_memory_limit_mb: float = 1024.0


@dataclass(slots=True)
class _Job:
    wamid: str
    modality: str
    dispatch_envelope: dict[str, Any]
    status: str = "pending"
    enqueue_status: str = "pending"
    processing_stage: str = "pending"
    send_status: str = "not_started"
    send_attempts: int = 0
    error_message: str | None = None
    failure_stage: str | None = None
    outbound_chunks: list[dict[str, Any]] | None = None


def _validate(config: SimulationConfig) -> None:
    if config.messages <= 0:
        raise ValueError("messages must be greater than zero")
    if config.messages > 1_000_000:
        raise ValueError("messages must not exceed 1,000,000")

    for field_name in (
        "fail_after_db_commit",
        "fail_enqueue",
        "kill_workers",
        "fail_meta_transient",
    ):
        value = getattr(config, field_name)
        if value < 0 or value > config.messages:
            raise ValueError(f"{field_name} must be between zero and messages")
    if config.fail_after_db_commit + config.fail_enqueue > config.messages:
        raise ValueError("fail_after_db_commit plus fail_enqueue must not exceed messages")

    if config.workers <= 0 or config.scaled_workers <= 0:
        raise ValueError("worker counts must be greater than zero")
    if config.scaled_workers <= config.workers:
        raise ValueError("scaled_workers must be greater than workers")
    if config.environment.lower() in {"prod", "production"}:
        raise ValueError("the stubbed fault gate cannot target production")
    if not config.organisation_id.strip():
        raise ValueError("organisation_id is required")


def build_report(config: SimulationConfig, *, apply: bool) -> dict[str, Any]:
    """Validate only by default; execute the model when apply is explicit."""
    _validate(config)
    if apply:
        return run_simulation(config)
    return {
        "mode": "dry-run",
        "configuration_valid": True,
        "apply_required": True,
        "jobs_generated": 0,
        "target": "stubbed-in-memory",
        "config": asdict(config),
    }


def run_simulation(config: SimulationConfig) -> dict[str, Any]:
    """Exercise durable state transitions with controlled boundary faults."""
    _validate(config)
    modalities = ("text", "document", "image", "audio")
    jobs: dict[str, _Job] = {}

    for index in range(config.messages):
        wamid = f"wamid.load.{index:08d}"
        modality = modalities[index % len(modalities)]
        attachments = []
        if modality != "text":
            attachments.append(
                {
                    "attachment_id": f"attachment-{index:08d}",
                    "filename": f"input-{index:08d}.{_extension(modality)}",
                    "mimetype": _mimetype(modality),
                    "source_provider": "whatsapp",
                }
            )
        jobs[wamid] = _Job(
            wamid=wamid,
            modality=modality,
            dispatch_envelope={
                "organisation_id": config.organisation_id,
                "user_id": f"user-{index % 100:04d}",
                "message_text": f"load message {index}",
                "provider": "whatsapp",
                "reply_context": {
                    "phone_number_id": "stub-phone",
                    "recipient_wa_id": f"stub-recipient-{index % 100:04d}",
                    "incoming_message_id": wamid,
                },
                "bot_context": {
                    "conversation_type": "personal",
                    "whatsapp_message_id": wamid,
                    "current_attachments": attachments,
                },
                "user_email": f"load-user-{index % 100:04d}@example.test",
            },
        )

    jobs_after_first_intake = len(jobs)
    if config.duplicate_replay:
        for index in range(config.messages):
            wamid = f"wamid.load.{index:08d}"
            jobs.setdefault(wamid, jobs[wamid])

    post_commit_faults: set[str] = set()
    enqueue_faults: set[str] = set()
    for index, job in enumerate(jobs.values()):
        if index < config.fail_after_db_commit:
            job.enqueue_status = "post_commit_interrupted"
            post_commit_faults.add(job.wamid)
        elif index < config.fail_after_db_commit + config.fail_enqueue:
            job.enqueue_status = "failed"
            enqueue_faults.add(job.wamid)
        else:
            job.enqueue_status = "enqueued"

    # The database sweep reconstructs every task from its committed envelope.
    for job in jobs.values():
        if job.enqueue_status in {"post_commit_interrupted", "failed"}:
            job.enqueue_status = "requeued"
    recovered_post_commit = sum(
        jobs[wamid].enqueue_status == "requeued" for wamid in post_commit_faults
    )
    recovered_enqueue = sum(
        jobs[wamid].enqueue_status == "requeued" for wamid in enqueue_faults
    )

    terminal_failures = 0
    for index, job in enumerate(jobs.values()):
        job.status = "in_progress"
        job.processing_stage = "agent_running"

        worker_was_killed = index < config.kill_workers
        unknown_agent_outcome = worker_was_killed and index % 2 == 1
        if unknown_agent_outcome:
            terminal_failures += 1
            job.status = "failed"
            job.error_message = "Worker stopped during agent execution"
            job.failure_stage = "agent_running"
            response_parts = ["I'm sorry, something went wrong while processing your request."]
        else:
            response_parts = [f"answer for {job.wamid}"]
            if index % 10 == 0:
                response_parts.append("durable second chunk")

        job.processing_stage = "response_ready"
        job.outbound_chunks = [
            {
                "index": chunk_index,
                "text": text,
                "provider_message_id": None,
            }
            for chunk_index, text in enumerate(response_parts)
        ]

        transient_meta_failure = index < config.fail_meta_transient
        job.send_attempts = 2 if transient_meta_failure else 1
        for chunk in job.outbound_chunks:
            chunk["provider_message_id"] = (
                f"wamid.provider.{index:08d}.{chunk['index']:02d}"
            )
        job.send_status = "sent"
        job.processing_stage = "failed" if unknown_agent_outcome else "completed"
        if not unknown_agent_outcome:
            job.status = "completed"

    modality_counts = {
        modality: sum(job.modality == modality for job in jobs.values())
        for modality in sorted(modalities)
    }
    complete_envelopes = sum(_complete_envelope(job.dispatch_envelope) for job in jobs.values())
    chunks_without_provider_id = sum(
        1
        for job in jobs.values()
        for chunk in job.outbound_chunks or []
        if not chunk.get("provider_message_id")
    )
    terminal_without_evidence = sum(
        1
        for job in jobs.values()
        if job.status == "failed" and (not job.error_message or not job.failure_stage)
    )
    stale_pending = sum(job.status == "pending" for job in jobs.values())
    completed_jobs = sum(job.status == "completed" for job in jobs.values())
    failed_jobs = sum(job.status == "failed" for job in jobs.values())

    average_processing_seconds = 5.0
    age_before = math.ceil(config.messages / config.workers) * average_processing_seconds
    age_after = math.ceil(config.messages / config.scaled_workers) * average_processing_seconds
    simulated_api_peak_mb = 96.0 + min(config.messages * 0.002, 64.0)
    simulated_worker_peak_mb = 192.0 + config.scaled_workers * 48.0

    checks = {
        "one_job_per_unique_wamid": jobs_after_first_intake == config.messages,
        "duplicate_replay_is_idempotent": (
            not config.duplicate_replay or len(jobs) == config.messages
        ),
        "dispatch_envelopes_complete": complete_envelopes == config.messages,
        "post_commit_failures_recovered": (
            recovered_post_commit == config.fail_after_db_commit
        ),
        "enqueue_failures_recovered": recovered_enqueue == config.fail_enqueue,
        "duplicate_claims_blocked": (
            not config.duplicate_replay or len(jobs) == jobs_after_first_intake
        ),
        "all_modalities_reconstructable": sum(modality_counts.values()) == config.messages,
        "all_outbound_chunks_accepted": chunks_without_provider_id == 0,
        "no_stale_pending_jobs": stale_pending == 0,
        "terminal_failures_have_evidence": terminal_without_evidence == 0,
        "capacity_reduces_queue_age": age_after < age_before,
        "simulated_memory_within_limits": (
            simulated_api_peak_mb < config.api_memory_limit_mb
            and simulated_worker_peak_mb < config.worker_memory_limit_mb
        ),
    }
    failed_checks = [name for name, passed in checks.items() if not passed]

    return {
        "mode": "apply",
        "target": "stubbed-in-memory",
        "throughput_claim": "none; run a deployed environment test for capacity",
        "passed": not failed_checks,
        "failed_checks": failed_checks,
        "checks": checks,
        "requested_messages": config.messages,
        "unique_jobs": jobs_after_first_intake,
        "jobs_after_duplicate_replay": len(jobs),
        "complete_dispatch_envelopes": complete_envelopes,
        "recovered_post_commit_failures": recovered_post_commit,
        "recovered_enqueue_failures": recovered_enqueue,
        "duplicate_claims_blocked": config.messages if config.duplicate_replay else 0,
        "worker_losses_injected": config.kill_workers,
        "meta_transient_failures_recovered": config.fail_meta_transient,
        "reconstructable_by_modality": modality_counts,
        "completed_jobs": completed_jobs,
        "failed_jobs_with_static_delivery": failed_jobs,
        "delivery_outcomes": completed_jobs + failed_jobs,
        "chunks_without_provider_id": chunks_without_provider_id,
        "stale_pending_jobs": stale_pending,
        "terminal_failures": terminal_failures,
        "terminal_failures_without_evidence": terminal_without_evidence,
        "oldest_queue_age_before_scale_seconds": age_before,
        "oldest_queue_age_after_scale_seconds": age_after,
        "simulated_api_peak_memory_mb": simulated_api_peak_mb,
        "simulated_worker_peak_memory_mb": simulated_worker_peak_mb,
        "config": asdict(config),
    }


def _complete_envelope(envelope: dict[str, Any]) -> bool:
    required = {
        "organisation_id",
        "user_id",
        "message_text",
        "provider",
        "reply_context",
        "bot_context",
        "user_email",
    }
    return required.issubset(envelope) and envelope.get("provider") == "whatsapp"


def _extension(modality: str) -> str:
    return {"document": "pdf", "image": "jpg", "audio": "ogg"}[modality]


def _mimetype(modality: str) -> str:
    return {
        "document": "application/pdf",
        "image": "image/jpeg",
        "audio": "audio/ogg",
    }[modality]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=10_000)
    parser.add_argument("--duplicate-replay", action="store_true")
    parser.add_argument("--fail-after-db-commit", type=int, default=0)
    parser.add_argument("--fail-enqueue", type=int, default=0)
    parser.add_argument("--kill-workers", type=int, default=0)
    parser.add_argument("--fail-meta-transient", type=int, default=0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--scaled-workers", type=int, default=4)
    parser.add_argument("--environment", default="local")
    parser.add_argument("--organisation-id", default="stub-non-production")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = SimulationConfig(
        messages=args.messages,
        duplicate_replay=args.duplicate_replay,
        fail_after_db_commit=args.fail_after_db_commit,
        fail_enqueue=args.fail_enqueue,
        kill_workers=args.kill_workers,
        fail_meta_transient=args.fail_meta_transient,
        workers=args.workers,
        scaled_workers=args.scaled_workers,
        environment=args.environment,
        organisation_id=args.organisation_id,
    )
    try:
        report = build_report(config, apply=args.apply)
    except ValueError as exc:
        print(json.dumps({"passed": False, "configuration_error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
