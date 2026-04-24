from unittest.mock import MagicMock, call, patch
from uuid import uuid4

from src.service.integrations.crm.orchestration.hubspot_initial_sync_chain import (
    build_hubspot_initial_sync_chain,
)


def test_build_hubspot_initial_sync_chain_orders_all_phases_then_finalize() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    chain_signature = MagicMock()
    failure_signature = MagicMock(name="failure_signature")

    with (
        patch(
            "src.service.integrations.crm.orchestration.hubspot_initial_sync_chain.task_signature",
            side_effect=[
                failure_signature,
                "schema",
                "pipelines",
                "users",
                "companies",
                "contacts",
                "deals",
                "tasks",
                "notes",
                "finalize",
            ],
        ) as mock_task_signature,
        patch(
            "src.service.integrations.crm.orchestration.hubspot_initial_sync_chain.chain",
            return_value=chain_signature,
        ) as mock_chain,
    ):
        build_hubspot_initial_sync_chain(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )

    kwargs = {
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
    }
    assert mock_task_signature.call_args_list == [
        call("integrations.mark_hubspot_initial_sync_failed", kwargs=kwargs),
        call("integrations.sync_hubspot_deal_schema", kwargs=kwargs),
        call("integrations.sync_hubspot_pipelines", kwargs=kwargs),
        call("integrations.sync_hubspot_users", kwargs=kwargs),
        call("integrations.sync_hubspot_companies", kwargs=kwargs),
        call("integrations.sync_hubspot_contacts", kwargs=kwargs),
        call("integrations.sync_hubspot_deals", kwargs=kwargs),
        call("integrations.sync_hubspot_tasks", kwargs=kwargs),
        call("integrations.sync_hubspot_notes", kwargs=kwargs),
        call("integrations.mark_hubspot_initial_sync_succeeded", kwargs=kwargs),
    ]
    mock_chain.assert_called_once_with(
        "schema",
        "pipelines",
        "users",
        "companies",
        "contacts",
        "deals",
        "tasks",
        "notes",
        "finalize",
    )
    chain_signature.set.assert_called_once_with(link_error=failure_signature)
