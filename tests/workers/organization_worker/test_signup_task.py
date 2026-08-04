from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.master_tables import SignupRequestStatus
from src.workers.organization_worker import client
from src.workers.organization_worker.celery_app import app
from src.workers.organization_worker.tasks import signup as _signup_task  # noqa: F401
from src.workers.organization_worker.tasks import signup as signup_task


@pytest.mark.asyncio
async def test_signup_dispatch_payload_contains_only_request_id(monkeypatch) -> None:
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        return "task-id"

    monkeypatch.setattr(client, "send_organization_task_async", send)
    signup_request_id = uuid4()

    task_id = await client.send_signup_provisioning_task(signup_request_id)

    assert task_id == "task-id"
    assert calls == [
        {
            "task_name": "organization.signup.provision",
            "kwargs": {"signup_request_id": str(signup_request_id)},
            "queue": "organization",
            "headers": None,
        }
    ]


def test_signup_task_is_registered_and_routed() -> None:
    assert "organization.signup.provision" in app.tasks
    assert app.conf.task_routes["organization.signup.provision"] == {
        "queue": "organization",
        "routing_key": "organization",
    }


@pytest.mark.asyncio
async def test_post_activation_follow_up_is_best_effort_and_records_research_failure(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    run_id = uuid4()
    marked_failed = []

    class Email:
        async def send_signup_welcome_email(self, *_args):
            raise RuntimeError("email unavailable")

    class Kai:
        def __init__(self, _session, _organisation_id):
            pass

        async def mark_pending(self):
            return None, True, run_id

        async def mark_failed(self, failed_run_id, message, *, clear_cooldown):
            marked_failed.append((failed_run_id, message, clear_cooldown))

    async def fail_research(*_args):
        raise RuntimeError("research unavailable")

    monkeypatch.setattr(signup_task, "EmailService", Email)
    monkeypatch.setattr(signup_task, "KaiTrainingService", Kai)
    monkeypatch.setattr(signup_task, "enforce_organisation_research_quota", lambda _id: None)
    monkeypatch.setattr(signup_task, "send_organization_research_task", fail_research)

    await signup_task._run_post_activation(
        SimpleNamespace(
            organisation_id=organisation_id,
            normalized_email="founder@acme.com",
            first_name="Ada",
        )
    )

    assert marked_failed == [(run_id, "research unavailable", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "expected"),
    [
        (
            SignupRequestStatus.CONTROL_PLANE_CREATED,
            ["tenant", "mark_tenant", "storage", "mark_storage", "activate", "follow_up"],
        ),
        (
            SignupRequestStatus.TENANT_DEFAULTS_CREATED,
            ["storage", "mark_storage", "activate", "follow_up"],
        ),
        (SignupRequestStatus.STORAGE_CREATED, ["activate", "follow_up"]),
        (SignupRequestStatus.ACTIVE, []),
    ],
)
async def test_signup_worker_resumes_without_repeating_completed_phases(
    monkeypatch,
    initial_status,
    expected,
) -> None:
    calls = []
    request_id = uuid4()
    organisation_id = uuid4()
    signup = SimpleNamespace(
        signup_request_id=request_id,
        organisation_id=organisation_id,
        normalized_email="founder@acme.com",
        first_name="Ada",
        status=initial_status,
    )
    organisation = SimpleNamespace(organisation_id=organisation_id)
    founder = object()

    @asynccontextmanager
    async def session_scope():
        yield object()

    class Service:
        def __init__(self, _session):
            pass

        async def ensure_control_plane(self, _request_id):
            return signup, organisation, object(), founder

        async def mark_tenant_defaults_created(self, _request_id):
            calls.append("mark_tenant")
            signup.status = SignupRequestStatus.TENANT_DEFAULTS_CREATED.value
            return signup

        async def mark_storage_created(self, _request_id):
            calls.append("mark_storage")
            signup.status = SignupRequestStatus.STORAGE_CREATED.value
            return signup

        async def activate(self, _request_id):
            calls.append("activate")
            signup.status = SignupRequestStatus.ACTIVE.value
            return signup

    class Provisioning:
        async def provision_founder_tenant(self, _organisation_id, _founder):
            calls.append("tenant")

        async def provision_storage(self, _organisation_id):
            calls.append("storage")

    async def follow_up(_signup):
        calls.append("follow_up")

    monkeypatch.setattr(signup_task, "master_async_session_scope", session_scope)
    monkeypatch.setattr(signup_task, "SignupService", Service)
    monkeypatch.setattr(signup_task, "OrganisationProvisioningService", Provisioning)
    monkeypatch.setattr(signup_task, "_run_post_activation", follow_up)

    result = await signup_task._provision_signup(request_id)

    assert calls == expected
    assert result["status"] == SignupRequestStatus.ACTIVE.value
