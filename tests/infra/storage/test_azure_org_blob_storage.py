from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage


def test_plan_crm_import_path_uses_import_scope_and_sanitizes_filename():
    storage = AzureOrganisationBlobStorage(uuid4())
    user_id = uuid4()
    import_id = uuid4()

    path = storage.plan_crm_import_path(
        user_id=user_id,
        import_id=import_id,
        original_filename=r"..\..\Q3 Accounts!.csv",
    )

    assert path == f"crm_imports/{user_id}/{import_id}/Q3_Accounts_.csv"


@pytest.mark.asyncio
async def test_generate_read_sas_reuses_container_check_and_delegation_key():
    """Repeated signing on one storage instance performs Azure setup only once."""
    storage = AzureOrganisationBlobStorage(uuid4())
    container_client = MagicMock()
    container_client.create_container = AsyncMock()
    blob_service = MagicMock()
    blob_service.get_container_client.return_value = container_client
    blob_service.get_user_delegation_key = AsyncMock(return_value=object())
    storage._get_blob_service_client = AsyncMock(return_value=blob_service)

    with patch(
        "src.infra.storage.azure_org_blob_storage.generate_blob_sas",
        return_value="signature",
    ):
        await storage.generate_read_sas("first.pdf", minutes=120)
        await storage.generate_read_sas("second.pdf", minutes=120)

    container_client.create_container.assert_awaited_once()
    blob_service.get_user_delegation_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_container_setup_failure_is_not_retried_for_every_sas():
    """A tolerated setup failure cannot fan out into one Azure call per file."""
    storage = AzureOrganisationBlobStorage(uuid4())
    container_client = MagicMock()
    container_client.create_container = AsyncMock(side_effect=RuntimeError("forbidden"))
    blob_service = MagicMock()
    blob_service.get_container_client.return_value = container_client
    blob_service.get_user_delegation_key = AsyncMock(return_value=object())
    storage._get_blob_service_client = AsyncMock(return_value=blob_service)

    with patch(
        "src.infra.storage.azure_org_blob_storage.generate_blob_sas",
        return_value="signature",
    ):
        await storage.generate_read_sas("first.pdf")
        await storage.generate_read_sas("second.pdf")

    container_client.create_container.assert_awaited_once()
