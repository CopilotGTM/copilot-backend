"""Contracts for immediate post-commit automation task delivery."""

from __future__ import annotations

import importlib
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import Session
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.infra.utils.async_runner import run_async


def test_commit_schedules_dispatch_but_rollback_drops_it(monkeypatch) -> None:
    """Only committed source transactions may become visible to the activation worker."""

    delivery = importlib.import_module("src.service.automation.events.delivery")
    published: list[tuple[str, str]] = []
    completed = Event()

    def publish(organisation_id, dispatch_id) -> bool:
        published.append((str(organisation_id), str(dispatch_id)))
        completed.set()
        return True

    monkeypatch.setattr(delivery, "_publish_staged_delivery", publish)
    organisation_id = uuid4()
    committed = AutomationEventDispatch(
        organisation_id=organisation_id,
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )
    committed_session = Session()
    delivery.stage_automation_event_delivery(
        SimpleNamespace(sync_session=committed_session), committed
    )

    assert published == []
    committed_session.commit()
    assert completed.wait(1)

    assert published == [(str(organisation_id), str(committed.dispatch_id))]

    rolled_back = AutomationEventDispatch(
        organisation_id=organisation_id,
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )
    rolled_back_session = Session()
    rolled_back_session.begin()
    delivery.stage_automation_event_delivery(
        SimpleNamespace(sync_session=rolled_back_session), rolled_back
    )
    rolled_back_session.rollback()

    assert published == [(str(organisation_id), str(committed.dispatch_id))]


def test_worker_loop_cleanup_cannot_cancel_post_commit_submission(monkeypatch) -> None:
    """Synchronous Celery wrappers may stop their loop immediately after the commit."""

    delivery = importlib.import_module("src.service.automation.events.delivery")
    submitted: list[object] = []
    monkeypatch.setattr(delivery, "_submit_staged_delivery", submitted.append)
    dispatch = AutomationEventDispatch(
        organisation_id=uuid4(),
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )

    async def commit_source() -> None:
        session = Session()
        delivery.stage_automation_event_delivery(SimpleNamespace(sync_session=session), dispatch)
        session.commit()

    run_async(commit_source())

    assert len(submitted) == 1


def test_submission_failure_does_not_fail_an_already_committed_write(monkeypatch) -> None:
    """Broker infrastructure cannot roll back data after PostgreSQL has committed it."""

    delivery = importlib.import_module("src.service.automation.events.delivery")

    def fail_submission(_item) -> None:
        raise RuntimeError("publisher is shutting down")

    monkeypatch.setattr(delivery, "_submit_staged_delivery", fail_submission)
    dispatch = AutomationEventDispatch(
        organisation_id=uuid4(),
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )
    session = Session()
    delivery.stage_automation_event_delivery(SimpleNamespace(sync_session=session), dispatch)

    session.commit()


def test_savepoints_wait_for_outer_commit_and_drop_only_the_failed_row(monkeypatch) -> None:
    """CRM imports commit each valid row savepoint before committing the whole batch."""

    delivery = importlib.import_module("src.service.automation.events.delivery")
    submitted: list[object] = []
    monkeypatch.setattr(delivery, "_submit_staged_delivery", submitted.append)
    session = Session()
    outer = session.begin()

    valid = AutomationEventDispatch(
        organisation_id=uuid4(),
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )
    valid_savepoint = session.begin_nested()
    delivery.stage_automation_event_delivery(SimpleNamespace(sync_session=session), valid)
    valid_savepoint.commit()

    assert submitted == []

    invalid = AutomationEventDispatch(
        organisation_id=uuid4(),
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )
    invalid_savepoint = session.begin_nested()
    delivery.stage_automation_event_delivery(SimpleNamespace(sync_session=session), invalid)
    invalid_savepoint.rollback()
    outer.commit()

    assert len(submitted) == 1
    assert submitted[0].dispatch_id == valid.dispatch_id


def test_direct_publish_retries_with_exponential_delays() -> None:
    """Transient Redis failures retry immediately without waiting for a database poller."""

    delivery = importlib.import_module("src.service.automation.events.delivery")
    attempts = 0
    delays: list[float] = []

    def publish(_task_name, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("redis unavailable")

    def sleep(delay: float) -> None:
        delays.append(delay)

    published = delivery.publish_task_sync_with_backoff(
        "chat.activate_automation_event",
        kwargs={"organisation_id": str(uuid4()), "dispatch_id": str(uuid4())},
        queue="chat_processing",
        publisher=publish,
        retry_delays=(1.0, 2.0, 4.0),
        sleep=sleep,
    )

    assert published
    assert attempts == 3
    assert delays == [1.0, 2.0]
