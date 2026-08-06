"""Contracts for immediate post-commit automation task delivery."""

from __future__ import annotations

import importlib
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import Session
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.infra.utils.async_runner import run_async


def _dispatch(organisation_id=None) -> AutomationEventDispatch:
    event_id = uuid4()
    return AutomationEventDispatch(
        event_id=event_id,
        organisation_id=organisation_id or uuid4(),
        event_key="crm.contact.created",
        route_mode="match",
        idempotency_key=f"crm.contact.created:{event_id}",
        envelope={},
    )


def test_commit_schedules_dispatch_but_rollback_drops_it(monkeypatch) -> None:
    """Only committed source transactions may become visible to the activation worker."""

    delivery = importlib.import_module("src.service.automation.events.delivery")
    published: list[tuple[str, str]] = []
    completed = Event()

    def publish(organisation_id, event_id) -> bool:
        """Build the publish fixture used by the surrounding regression cases."""
        published.append((str(organisation_id), str(event_id)))
        completed.set()
        return True

    monkeypatch.setattr(delivery, "_publish_staged_delivery", publish)
    organisation_id = uuid4()
    committed = _dispatch(organisation_id)
    committed_session = Session()
    delivery.stage_automation_event_delivery(
        SimpleNamespace(sync_session=committed_session), committed
    )

    assert published == []
    committed_session.commit()
    assert completed.wait(1)

    assert published == [(str(organisation_id), str(committed.event_id))]

    rolled_back = _dispatch(organisation_id)
    rolled_back_session = Session()
    rolled_back_session.begin()
    delivery.stage_automation_event_delivery(
        SimpleNamespace(sync_session=rolled_back_session), rolled_back
    )
    rolled_back_session.rollback()

    assert published == [(str(organisation_id), str(committed.event_id))]


def test_worker_loop_cleanup_cannot_cancel_post_commit_submission(monkeypatch) -> None:
    """Synchronous Celery wrappers may stop their loop immediately after the commit."""

    delivery = importlib.import_module("src.service.automation.events.delivery")
    submitted: list[object] = []
    monkeypatch.setattr(delivery, "_submit_staged_delivery", submitted.append)
    dispatch = _dispatch()

    async def commit_source() -> None:
        """Build the commit source fixture used by the surrounding regression cases."""
        session = Session()
        delivery.stage_automation_event_delivery(SimpleNamespace(sync_session=session), dispatch)
        session.commit()

    run_async(commit_source())

    assert len(submitted) == 1


def test_submission_failure_does_not_fail_an_already_committed_write(monkeypatch) -> None:
    """Broker infrastructure cannot roll back data after PostgreSQL has committed it."""

    delivery = importlib.import_module("src.service.automation.events.delivery")

    def fail_submission(_item) -> None:
        """Build the fail submission fixture used by the surrounding regression cases."""
        raise RuntimeError("publisher is shutting down")

    monkeypatch.setattr(delivery, "_submit_staged_delivery", fail_submission)
    dispatch = _dispatch()
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

    valid = _dispatch()
    valid_savepoint = session.begin_nested()
    delivery.stage_automation_event_delivery(SimpleNamespace(sync_session=session), valid)
    valid_savepoint.commit()

    assert submitted == []

    invalid = _dispatch()
    invalid_savepoint = session.begin_nested()
    delivery.stage_automation_event_delivery(SimpleNamespace(sync_session=session), invalid)
    invalid_savepoint.rollback()
    outer.commit()

    assert len(submitted) == 1
    assert submitted[0].event_id == valid.event_id


def test_direct_publish_retries_with_exponential_delays() -> None:
    """Transient Redis failures retry immediately without waiting for a database poller."""

    delivery = importlib.import_module("src.service.automation.events.delivery")
    attempts = 0
    delays: list[float] = []

    def publish(_task_name, **_kwargs):
        """Build the publish fixture used by the surrounding regression cases."""
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("redis unavailable")

    def sleep(delay: float) -> None:
        """Build the sleep fixture used by the surrounding regression cases."""
        delays.append(delay)

    published = delivery.publish_task_sync_with_backoff(
        "chat.activate_automation_event",
        kwargs={"organisation_id": str(uuid4()), "event_id": str(uuid4())},
        queue="chat_processing",
        publisher=publish,
        retry_delays=(1.0, 2.0, 4.0),
        sleep=sleep,
    )

    assert published
    assert attempts == 3
    assert delays == [1.0, 2.0]
