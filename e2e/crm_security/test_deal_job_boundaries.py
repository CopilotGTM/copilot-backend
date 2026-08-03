"""Deployed CRM job scenarios covering authorization, identity, workers, and SSE."""

from __future__ import annotations

from uuid import UUID

import pytest

from e2e.crm_security.support import (
    CRMActor,
    configured_uuid,
    count_jobs_since,
    fetch_job,
    get,
    post,
    read_job_stream,
    require_crm_live_target,
    run_started_at,
    wait_for_terminal_job,
)

pytestmark = pytest.mark.e2e


def _job_id(response) -> UUID:
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "SUCCESS"
    return UUID(payload["data"]["job_id"])


def test_shared_deal_summary_reuses_authorized_job_and_reaches_durable_output() -> None:
    """Prove CRM-E2E-001 from public generation through shared SSE and durable output.

    Actors: owner A, collaborator B, same-org non-collaborator C, foreign user D.
    Records: the dedicated primary deal and its generated deal-summary job.
    Forbidden outcome: C or D creates or reads work for the protected deal.
    Cleanup: durable job history is retained on the dedicated E2E deal.
    """
    require_crm_live_target()
    owner = CRMActor.from_environment("owner_a", "E2E_CRM_OWNER_A")
    collaborator = CRMActor.from_environment("collaborator_b", "E2E_CRM_COLLABORATOR_B")
    member = CRMActor.from_environment("member_c", "E2E_CRM_MEMBER_C")
    foreign = CRMActor.from_environment("foreign_d", "E2E_CRM_FOREIGN_D")
    organisation_id = configured_uuid("E2E_CRM_OWNER_ORGANISATION_ID")
    deal_id = configured_uuid("E2E_CRM_PRIMARY_DEAL_ID")
    started_at = run_started_at()

    path = f"/users/crm/deals/{deal_id}/deal-summary/generate"
    job_id = _job_id(post(owner, path))
    owner_job = fetch_job(organisation_id, job_id)
    assert owner_job is not None
    assert owner_job["organisation_id"] == organisation_id
    assert owner_job["deal_id"] == deal_id
    assert owner_job["event_id"] is None
    assert owner_job["job_type"] == "deal_summary"

    assert _job_id(post(collaborator, path)) == job_id
    count_before_denials = count_jobs_since(organisation_id, deal_id, since=started_at)
    assert post(member, path).status_code == 404
    assert post(foreign, path).status_code == 404
    assert count_jobs_since(organisation_id, deal_id, since=started_at) == count_before_denials

    stream_path = f"/users/crm/deals/{deal_id}/deal-summary/stream/{job_id}"
    events = read_job_stream(collaborator, stream_path)
    assert events[0].event == "ready"
    assert events[-1].event == "complete"
    assert any(event.event == "frame" for event in events)

    job = wait_for_terminal_job(organisation_id, job_id)
    assert job["status"] == "completed", job["error_message"]
    assert job["result_data"]

    replay_from = next((event.event_id for event in reversed(events) if event.event_id), "0-0")
    replay = read_job_stream(collaborator, stream_path, last_event_id=replay_from)
    assert replay[0].event == "ready"
    assert replay[-1].event == "complete"
    assert any(event.event == "frame" for event in replay)


def test_meeting_stream_binds_deal_event_and_supported_job_type() -> None:
    """Prove CRM-E2E-002 rejects deal/event/type substitution around one real job.

    Actors: owner A and collaborator B.
    Records: primary deal/event, secondary deal/event, and one briefing job.
    Forbidden outcome: a valid job streams through a mismatched route identity.
    Cleanup: durable job history is retained on the dedicated E2E deal.
    """
    require_crm_live_target()
    owner = CRMActor.from_environment("owner_a", "E2E_CRM_OWNER_A")
    collaborator = CRMActor.from_environment("collaborator_b", "E2E_CRM_COLLABORATOR_B")
    organisation_id = configured_uuid("E2E_CRM_OWNER_ORGANISATION_ID")
    deal_id = configured_uuid("E2E_CRM_PRIMARY_DEAL_ID")
    event_id = configured_uuid("E2E_CRM_PRIMARY_EVENT_ID")
    secondary_event_id = configured_uuid("E2E_CRM_SECONDARY_EVENT_ID")
    started_at = run_started_at()

    create_path = f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/briefing"
    job_id = _job_id(post(owner, create_path))
    job = fetch_job(organisation_id, job_id)
    assert job is not None
    assert (job["deal_id"], job["event_id"], job["job_type"]) == (
        deal_id,
        event_id,
        "meeting_briefing",
    )

    count_before_denials = count_jobs_since(organisation_id, deal_id, since=started_at)
    wrong_event_create = post(
        owner,
        f"/users/crm/deals/{deal_id}/meetings/{secondary_event_id}/jobs/briefing",
    )
    assert wrong_event_create.status_code == 404
    assert (
        post(owner, f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/followup").status_code
        == 400
    )
    assert count_jobs_since(organisation_id, deal_id, since=started_at) == count_before_denials

    assert (
        get(
            collaborator,
            f"/users/crm/deals/{deal_id}/meetings/{secondary_event_id}/jobs/{job_id}/stream",
        ).status_code
        == 403
    )
    assert (
        get(
            collaborator,
            f"/users/crm/deals/{deal_id}/deal-summary/stream/{job_id}",
        ).status_code
        == 403
    )

    stream_path = f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/{job_id}/stream"
    events = read_job_stream(collaborator, stream_path)
    assert events[0].event == "ready"
    assert events[-1].event == "complete"
    terminal_job = wait_for_terminal_job(organisation_id, job_id)
    assert terminal_job["status"] == "completed", terminal_job["error_message"]
