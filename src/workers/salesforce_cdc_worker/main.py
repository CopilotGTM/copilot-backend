"""Process entrypoint for the single Salesforce aggregate CDC worker pod."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime

from src.infra.observability.otel import async_init_otel
from src.infra.observability.sentry import init_sentry
from src.workers.salesforce_cdc_worker.config import SalesforceCdcWorkerSettings
from src.workers.salesforce_cdc_worker.healthcheck import LIVE_PATH, READY_PATH
from src.workers.salesforce_cdc_worker.supervisor import SalesforceCdcSupervisor


async def run_worker() -> None:
    """Initialize telemetry, signals, health files, and the connector supervisor."""
    logging.basicConfig(level=logging.INFO)
    init_sentry()
    await async_init_otel("-salesforce-cdc-worker")
    supervisor = SalesforceCdcSupervisor(SalesforceCdcWorkerSettings.from_env())
    loop = asyncio.get_running_loop()
    for process_signal in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(process_signal, supervisor.request_shutdown)

    health_task = asyncio.create_task(_write_health(supervisor), name="salesforce-cdc-health")
    try:
        await supervisor.run()
    finally:
        health_task.cancel()
        with suppress(asyncio.CancelledError):
            await health_task


async def _write_health(supervisor: SalesforceCdcSupervisor) -> None:
    """Refresh liveness and atomically publish readiness from the event loop."""
    ready_tmp = READY_PATH.with_suffix(".tmp")
    while True:
        now = datetime.now(UTC).isoformat()
        LIVE_PATH.write_text(now, encoding="utf-8")
        ready_tmp.write_text(json.dumps(supervisor.readiness_state()), encoding="utf-8")
        ready_tmp.replace(READY_PATH)
        await asyncio.sleep(10)


def main() -> None:
    """Run the worker until SIGTERM or SIGINT requests graceful shutdown."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
