"""Regression tests for sync-to-async worker loop management."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.infra.utils import async_runner


def test_shared_loop_overlaps_four_coroutines_and_preserves_context():
    """Each Celery thread must overlap I/O without leaking request context."""
    request_id = contextvars.ContextVar("test_request_id")
    started = 0
    all_started: asyncio.Event | None = None

    async def overlap() -> tuple[str, int]:
        nonlocal all_started, started
        if all_started is None:
            all_started = asyncio.Event()
        started += 1
        if started == 4:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        await asyncio.sleep(0.02)
        return request_id.get(), id(asyncio.get_running_loop())

    def run(index: int) -> tuple[str, int]:
        request_id.set(f"request-{index}")
        return async_runner.run_async(overlap())

    async_runner.start_shared_loop()
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(run, range(4)))
    finally:
        async_runner.stop_shared_loop()

    assert {request for request, _loop in results} == {
        "request-0",
        "request-1",
        "request-2",
        "request-3",
    }
    assert len({loop for _request, loop in results}) == 1


def test_shared_loop_timeout_waits_for_coroutine_cleanup():
    """A timed-out task must finish cancellation cleanup before Celery can ack it."""
    cleanup_finished = threading.Event()
    late_write = threading.Event()

    async def operation() -> None:
        try:
            await asyncio.sleep(10)
            late_write.set()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.set()

    async_runner.start_shared_loop()
    try:
        with pytest.raises(async_runner.WorkerAsyncTimeoutError):
            async_runner.run_async(operation(), timeout_seconds=0.01)
    finally:
        async_runner.stop_shared_loop()

    assert cleanup_finished.is_set()
    assert not late_write.is_set()


def test_one_timeout_does_not_cancel_a_sibling_coroutine():
    """Cancellation belongs to one Celery message and must not affect a sibling."""
    sibling_started = threading.Event()

    async def slow() -> None:
        await asyncio.sleep(10)

    async def sibling() -> str:
        sibling_started.set()
        await asyncio.sleep(0.03)
        return "completed"

    async_runner.start_shared_loop()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            sibling_result = executor.submit(async_runner.run_async, sibling())
            assert sibling_started.wait(timeout=1)
            timed_out = executor.submit(
                async_runner.run_async,
                slow(),
                timeout_seconds=0.01,
            )
            with pytest.raises(async_runner.WorkerAsyncTimeoutError):
                timed_out.result(timeout=1)
            assert sibling_result.result(timeout=1) == "completed"
    finally:
        async_runner.stop_shared_loop()


def test_provider_timeout_error_is_not_relabelled_as_worker_deadline():
    """Provider timeouts remain retryable provider failures for task handlers."""

    async def provider_call() -> None:
        raise TimeoutError("provider socket timed out")

    async_runner.start_shared_loop()
    try:
        with pytest.raises(TimeoutError, match="provider socket timed out") as exc_info:
            async_runner.run_async(provider_call(), timeout_seconds=1)
    finally:
        async_runner.stop_shared_loop()

    assert not isinstance(exc_info.value, async_runner.WorkerAsyncTimeoutError)


def test_prefork_provider_timeout_error_is_not_relabelled_as_worker_deadline():
    """The persistent-loop fallback must preserve provider timeout semantics too."""

    async def provider_call() -> None:
        raise TimeoutError("provider socket timed out")

    try:
        with pytest.raises(TimeoutError, match="provider socket timed out") as exc_info:
            async_runner.run_async(provider_call(), timeout_seconds=1)
    finally:
        loop = getattr(async_runner._thread_local, "loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        if hasattr(async_runner._thread_local, "loop"):
            del async_runner._thread_local.loop

    assert not isinstance(exc_info.value, async_runner.WorkerAsyncTimeoutError)


def test_shared_loop_health_ticks_until_shutdown():
    """The loop heartbeat must prove the async owner thread is still advancing."""
    async_runner.start_shared_loop()
    try:
        before = async_runner.get_shared_loop_health()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            after = async_runner.get_shared_loop_health()
            if after.last_tick_monotonic > before.last_tick_monotonic:
                break
            time.sleep(0.01)
        else:
            pytest.fail("shared loop heartbeat did not advance")

        assert after.running is True
    finally:
        async_runner.stop_shared_loop()

    assert async_runner.get_shared_loop_health().running is False


def test_shared_loop_refreshes_probe_heartbeat_file(tmp_path, monkeypatch):
    heartbeat = tmp_path / "worker-asyncio-loop-heartbeat"
    monkeypatch.setattr(async_runner, "_SHARED_LOOP_HEARTBEAT_PATH", heartbeat)

    async_runner.start_shared_loop()
    try:
        deadline = time.monotonic() + 1
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert heartbeat.exists()
    finally:
        async_runner.stop_shared_loop()

    assert not heartbeat.exists()


def test_shared_loop_runs_thread_pool_coroutines_on_one_event_loop():
    """Threaded Celery tasks must share one loop so async pools stay loop-safe."""
    started = 0
    started_lock = threading.Lock()
    both_started = threading.Event()

    async def overlap() -> int:
        """Wait until both task threads have entered their coroutine."""
        nonlocal started
        with started_lock:
            started += 1
            if started == 2:
                both_started.set()

        while not both_started.is_set():
            await asyncio.sleep(0.001)
        return id(asyncio.get_running_loop())

    async_runner.start_shared_loop()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            loop_ids = list(
                executor.map(lambda _index: async_runner.run_async(overlap()), range(2))
            )
    finally:
        async_runner.stop_shared_loop()

    assert len(set(loop_ids)) == 1


def test_reset_after_fork_discards_inherited_thread_local_loop():
    """Prefork children must not reuse the parent event loop or executor."""
    parent_loop = asyncio.new_event_loop()
    async_runner._thread_local.loop = parent_loop

    async_runner.reset_after_fork()

    async def current_loop_id() -> int:
        """Return the loop created after the fork reset."""
        return id(asyncio.get_running_loop())

    try:
        child_loop_id = async_runner.run_async(current_loop_id())
    finally:
        loop = getattr(async_runner._thread_local, "loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        if not parent_loop.is_closed():
            parent_loop.close()
        if hasattr(async_runner._thread_local, "loop"):
            del async_runner._thread_local.loop

    assert child_loop_id != id(parent_loop)


def test_reset_after_fork_discards_inherited_shared_loop(mocker):
    """A prefork child must not submit work to the parent's vanished loop thread."""
    inherited_loop = mocker.Mock()
    inherited_thread = mocker.Mock()
    async_runner._shared_loop = inherited_loop
    async_runner._shared_loop_thread = inherited_thread

    async_runner.reset_after_fork()

    assert async_runner._shared_loop is None
    assert async_runner._shared_loop_thread is None


def test_reset_after_fork_replaces_inherited_state_lock():
    """A lock held by a vanished parent thread must not deadlock the child."""
    inherited_lock = async_runner._shared_loop_state_lock

    async_runner.reset_after_fork()

    assert async_runner._shared_loop_state_lock is not inherited_lock


def test_reset_after_fork_clears_asyncio_policy_loop():
    """Fork reset must also detach the loop visible through asyncio policy APIs."""
    parent_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(parent_loop)

    async_runner.reset_after_fork()

    try:
        with pytest.raises(RuntimeError, match="There is no current event loop"):
            asyncio.get_event_loop()
    finally:
        loop = getattr(async_runner._thread_local, "loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        if not parent_loop.is_closed():
            parent_loop.close()
        asyncio.set_event_loop(None)


def test_reset_after_fork_closes_policy_loop_without_thread_local_loop():
    """Policy-only inherited loops should be closed, not only detached."""
    parent_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(parent_loop)
    if hasattr(async_runner._thread_local, "loop"):
        del async_runner._thread_local.loop

    async_runner.reset_after_fork()

    try:
        assert parent_loop.is_closed()
    finally:
        if not parent_loop.is_closed():
            parent_loop.close()
        asyncio.set_event_loop(None)


def test_reset_after_fork_clears_database_provider_caches(mocker):
    """Fork reset must keep DB engine cleanup coupled to loop cleanup."""
    async_clear = mocker.patch("src.infra.utils.async_runner._reset_async_db_provider_after_fork")
    sync_clear = mocker.patch("src.infra.utils.async_runner._clear_sync_db_provider_cache")

    async_runner.reset_after_fork()

    async_clear.assert_called_once_with()
    sync_clear.assert_called_once_with()


def test_async_cache_clear_awaits_disposal_on_the_current_loop(monkeypatch):
    """An open owning loop must gracefully close its asyncpg connections."""

    calls: list[asyncio.AbstractEventLoop] = []

    async def close_provider() -> None:
        """Record the loop used for active provider disposal."""

        calls.append(asyncio.get_running_loop())

    monkeypatch.setattr(
        "src.infra.database.factory.close_async_database_provider",
        close_provider,
        raising=False,
    )
    loop = asyncio.new_event_loop()
    try:
        async_runner._clear_async_db_provider_cache(loop)
    finally:
        loop.close()

    assert calls == [loop]


def test_async_cache_clear_uses_recovery_loop_when_owner_is_closed(monkeypatch):
    """A closed owner loop still needs deterministic active pool disposal."""

    calls: list[asyncio.AbstractEventLoop] = []

    async def close_provider() -> None:
        """Record the temporary loop used after the owner was closed."""

        calls.append(asyncio.get_running_loop())

    monkeypatch.setattr(
        "src.infra.database.factory.close_async_database_provider",
        close_provider,
        raising=False,
    )
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()

    async_runner._clear_async_db_provider_cache(closed_loop)

    assert len(calls) == 1
    assert calls[0] is not closed_loop
    assert calls[0].is_closed()


def test_async_fork_reset_delegates_to_passive_factory_cleanup(mocker):
    """Fork cleanup must preserve the parent's underlying connections."""

    reset = mocker.patch(
        "src.infra.database.factory.reset_async_database_provider_after_fork",
    )

    async_runner._reset_async_db_provider_after_fork()

    reset.assert_called_once_with()


def test_sync_cache_clear_delegates_to_database_factory(mocker):
    """Fork cleanup must delegate sync-pool ownership to the factory."""

    reset = mocker.patch(
        "src.infra.database.factory.reset_sync_database_provider_after_fork",
    )

    async_runner._clear_sync_db_provider_cache()

    reset.assert_called_once_with()
