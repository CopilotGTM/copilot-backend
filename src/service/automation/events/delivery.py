"""Direct post-commit delivery for durable automation dispatches.

CRM and future provider producers stage an identity on their SQLAlchemy session.
The commit event publishes only after the source transaction succeeds, so an
activation worker cannot observe rolled-back source data.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import sleep as blocking_sleep
from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.infra.messaging.task_dispatcher import send_task, send_task_async
from src.infra.utils.logger import get_logger

logger = get_logger("automation_event_delivery")

Publisher = Callable[..., Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]
SyncPublisher = Callable[..., Any]
SyncSleeper = Callable[[float], None]
DEFAULT_PUBLISH_RETRY_DELAYS = (1.0, 2.0, 4.0)

_PENDING_DELIVERIES_KEY = "automation_event_deliveries"
_NESTED_DELIVERIES_KEY = "automation_event_nested_deliveries"
_ROLLED_BACK_TRANSACTIONS_KEY = "automation_event_rolled_back_transactions"
_delivery_executor: ThreadPoolExecutor | None = None
_delivery_executor_lock = Lock()
_delivery_futures: set[Future[bool]] = set()


@dataclass(frozen=True)
class _StagedDelivery:
    organisation_id: UUID
    dispatch_id: UUID


async def publish_task_with_backoff(
    task_name: str,
    *,
    kwargs: dict[str, str],
    queue: str,
    publisher: Publisher = send_task_async,
    retry_delays: tuple[float, ...] = DEFAULT_PUBLISH_RETRY_DELAYS,
    sleep: Sleeper = asyncio.sleep,
) -> bool:
    """Publish one automation task with bounded exponential retry delays."""

    for attempt in range(len(retry_delays) + 1):
        try:
            await publisher(
                task_name,
                kwargs=kwargs,
                queue=queue,
                routing_key=queue,
            )
            return True
        except Exception:
            if attempt == len(retry_delays):
                logger.exception(
                    "Automation task publication exhausted retries task=%s queue=%s",
                    task_name,
                    queue,
                )
                return False
            delay = retry_delays[attempt]
            logger.warning(
                "Automation task publication will retry task=%s queue=%s "
                "attempt=%d delay_seconds=%.1f",
                task_name,
                queue,
                attempt + 1,
                delay,
            )
            await sleep(delay)
    return False


def publish_task_sync_with_backoff(
    task_name: str,
    *,
    kwargs: dict[str, str],
    queue: str,
    publisher: SyncPublisher = send_task,
    retry_delays: tuple[float, ...] = DEFAULT_PUBLISH_RETRY_DELAYS,
    sleep: SyncSleeper = blocking_sleep,
) -> bool:
    """Publish from a commit callback without depending on its event-loop lifetime."""

    for attempt in range(len(retry_delays) + 1):
        try:
            publisher(
                task_name,
                kwargs=kwargs,
                queue=queue,
                routing_key=queue,
            )
            return True
        except Exception:
            if attempt == len(retry_delays):
                logger.exception(
                    "Direct automation publication exhausted retries task=%s queue=%s",
                    task_name,
                    queue,
                )
                return False
            delay = retry_delays[attempt]
            logger.warning(
                "Direct automation publication will retry task=%s queue=%s "
                "attempt=%d delay_seconds=%.1f",
                task_name,
                queue,
                attempt + 1,
                delay,
            )
            sleep(delay)
    return False


def stage_automation_event_delivery(
    session: AsyncSession,
    dispatch: AutomationEventDispatch,
) -> None:
    """Stage one dispatch for publication after this session successfully commits."""

    sync_session = session.sync_session
    nested = sync_session.get_nested_transaction()
    if nested is None:
        pending: dict[tuple[UUID, UUID], _StagedDelivery] = sync_session.info.setdefault(
            _PENDING_DELIVERIES_KEY, {}
        )
    else:
        nested_pending: dict[SessionTransaction, dict[tuple[UUID, UUID], _StagedDelivery]] = (
            sync_session.info.setdefault(_NESTED_DELIVERIES_KEY, {})
        )
        pending = nested_pending.setdefault(nested, {})
    identity = (dispatch.organisation_id, dispatch.dispatch_id)
    pending[identity] = _StagedDelivery(*identity)


def _publish_staged_delivery(organisation_id: UUID, dispatch_id: UUID) -> bool:
    """Deliver the minimal identity needed by the organization-scoped worker."""

    return publish_task_sync_with_backoff(
        "chat.activate_automation_event",
        kwargs={
            "organisation_id": str(organisation_id),
            "dispatch_id": str(dispatch_id),
        },
        queue="chat_processing",
    )


def _get_delivery_executor() -> ThreadPoolExecutor:
    global _delivery_executor

    if _delivery_executor is None:
        with _delivery_executor_lock:
            if _delivery_executor is None:
                # Lazy initialization keeps Celery's parent process thread-free;
                # prefork children must not inherit a started publisher pool.
                _delivery_executor = ThreadPoolExecutor(
                    max_workers=4,
                    thread_name_prefix="automation_delivery",
                )
    return _delivery_executor


def _submit_staged_delivery(item: _StagedDelivery) -> None:
    future = _get_delivery_executor().submit(
        _publish_staged_delivery,
        item.organisation_id,
        item.dispatch_id,
    )
    # The set keeps queued work alive until its final broker attempt completes.
    _delivery_futures.add(future)
    future.add_done_callback(_delivery_futures.discard)


def _deliver_after_commit(session: Session) -> None:
    # SQLAlchemy also emits after_commit for SAVEPOINT release. Import rows must
    # wait for the outer batch commit before their workers can observe them.
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_PENDING_DELIVERIES_KEY, {})
    for item in pending.values():
        try:
            _submit_staged_delivery(item)
        except Exception:
            # PostgreSQL has already committed. Report the delivery failure but
            # never return a false write failure that callers may retry blindly.
            logger.exception(
                "Could not submit committed automation dispatch org=%s dispatch=%s",
                item.organisation_id,
                item.dispatch_id,
            )
    session.info.pop(_NESTED_DELIVERIES_KEY, None)
    session.info.pop(_ROLLED_BACK_TRANSACTIONS_KEY, None)


def _drop_after_rollback(session: Session) -> None:
    nested = session.get_nested_transaction()
    if nested is not None:
        rolled_back: set[SessionTransaction] = session.info.setdefault(
            _ROLLED_BACK_TRANSACTIONS_KEY, set()
        )
        rolled_back.add(nested)
        return

    # A rolled-back outer transaction has no dispatch rows. Publishing their
    # generated UUIDs would create tasks that can never resolve source records.
    session.info.pop(_PENDING_DELIVERIES_KEY, None)
    session.info.pop(_NESTED_DELIVERIES_KEY, None)
    session.info.pop(_ROLLED_BACK_TRANSACTIONS_KEY, None)


def _finish_nested_transaction(
    session: Session,
    transaction: SessionTransaction,
) -> None:
    if not transaction.nested:
        return
    nested_pending: dict[SessionTransaction, dict[tuple[UUID, UUID], _StagedDelivery]] = (
        session.info.get(_NESTED_DELIVERIES_KEY, {})
    )
    pending = nested_pending.pop(transaction, {})
    rolled_back: set[SessionTransaction] = session.info.get(_ROLLED_BACK_TRANSACTIONS_KEY, set())
    if transaction in rolled_back:
        rolled_back.discard(transaction)
        return

    parent = transaction.parent
    if parent is not None and parent.nested:
        nested_pending.setdefault(parent, {}).update(pending)
    else:
        root_pending: dict[tuple[UUID, UUID], _StagedDelivery] = session.info.setdefault(
            _PENDING_DELIVERIES_KEY, {}
        )
        root_pending.update(pending)


# TODO(nikhil, 2026-07-22): Add low-frequency recovery for dispatch rows left
# pending after the process exits or all direct broker publication retries fail.
if not event.contains(Session, "after_commit", _deliver_after_commit):
    event.listen(Session, "after_commit", _deliver_after_commit)
if not event.contains(Session, "after_rollback", _drop_after_rollback):
    event.listen(Session, "after_rollback", _drop_after_rollback)
if not event.contains(Session, "after_transaction_end", _finish_nested_transaction):
    event.listen(Session, "after_transaction_end", _finish_nested_transaction)
