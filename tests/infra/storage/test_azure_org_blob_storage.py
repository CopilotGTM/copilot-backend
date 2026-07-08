from uuid import uuid4

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
