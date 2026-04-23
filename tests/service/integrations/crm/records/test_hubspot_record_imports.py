from src.service.integrations.crm.records.hubspot.companies.company_sync_service import (
    HubSpotCompanyRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_sync_service import (
    HubSpotContactRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.deals.deal_sync_service import (
    HubSpotDealRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.pipelines.pipeline_sync_service import (
    HubSpotPipelineRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.users.user_sync_service import (
    HubSpotUserRecordSyncService,
)


def test_hubspot_record_services_import_from_provider_packages() -> None:
    assert HubSpotPipelineRecordSyncService is not None
    assert HubSpotUserRecordSyncService is not None
    assert HubSpotCompanyRecordSyncService is not None
    assert HubSpotContactRecordSyncService is not None
    assert HubSpotDealRecordSyncService is not None
