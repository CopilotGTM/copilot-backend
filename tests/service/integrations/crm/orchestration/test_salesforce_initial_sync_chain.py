from unittest.mock import MagicMock, call, patch
from uuid import uuid4

from src.service.integrations.crm.orchestration.salesforce_initial_sync_chain import (
    build_salesforce_initial_sync_chain,
)


def test_build_salesforce_initial_sync_chain_orders_schema_then_pipelines_then_users_then_finalize() -> (
    None
):
    organisation_id = uuid4()
    integration_id = uuid4()
    chain_signature = MagicMock()
    failure_signature = MagicMock(name="failure_signature")

    with (
        patch(
            "src.service.integrations.crm.orchestration.salesforce_initial_sync_chain.task_signature",
            side_effect=[
                "schema",
                "pipelines",
                "users",
                "finalize",
                failure_signature,
            ],
        ) as mock_task_signature,
        patch(
            "src.service.integrations.crm.orchestration.salesforce_initial_sync_chain.chain",
            return_value=chain_signature,
        ) as mock_chain,
    ):
        build_salesforce_initial_sync_chain(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )

    kwargs = {
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
    }
    assert mock_task_signature.call_args_list == [
        call("integrations.sync_salesforce_opportunity_schema", kwargs=kwargs),
        call("integrations.sync_salesforce_pipelines", kwargs=kwargs),
        call("integrations.sync_salesforce_users", kwargs=kwargs),
        call("integrations.mark_salesforce_initial_sync_succeeded", kwargs=kwargs),
        call("integrations.mark_salesforce_initial_sync_failed", kwargs=kwargs),
    ]
    mock_chain.assert_called_once_with("schema", "pipelines", "users", "finalize")
    chain_signature.set.assert_called_once_with(link_error=failure_signature)
