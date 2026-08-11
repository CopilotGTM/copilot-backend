import subprocess
import sys
from uuid import uuid4

import pytest
from src.service.integrations.webhooks.salesforce import change_processor
from src.service.integrations.webhooks.salesforce.change_processor import (
    SalesforceCdcChangeProcessor,
)


@pytest.mark.asyncio
async def test_processor_routes_opportunity_without_importing_celery_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed: list[str] = []

    async def capture_record(*, record_id: str, **_kwargs: object) -> None:
        processed.append(record_id)

    monkeypatch.setitem(change_processor._HANDLERS, "Opportunity", capture_record)

    await SalesforceCdcChangeProcessor().process(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        entity_name="Opportunity",
        record_id="006xx0000000001AAA",
        change_type="UPDATE",
        instance_url="https://acme.my.salesforce.com",
    )

    assert processed == ["006xx0000000001AAA"]


def test_change_processor_import_does_not_load_celery_subscriber() -> None:
    task_module = (
        "src.workers.integrations_worker.tasks.crm.salesforce.salesforce_cdc_subscriber_task"
    )
    # A fresh interpreter proves the import boundary independently of pytest collection order.
    subprocess.run(  # noqa: S603 - executable and script are process-owned constants
        [
            sys.executable,
            "-c",
            f"import sys; import src.service.integrations.webhooks.salesforce.change_processor; "
            f"assert {task_module!r} not in sys.modules",
        ],
        check=True,
    )
