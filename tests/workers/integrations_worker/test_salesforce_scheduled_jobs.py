import pytest
from src.workers.integrations_worker.scheduled_jobs import get_scheduled_job


def test_get_scheduled_job_returns_salesforce_schema_schedule_definition() -> None:
    job = get_scheduled_job("sync-salesforce-opportunity-schema-all-orgs")

    assert job.name == "sync-salesforce-opportunity-schema-all-orgs"
    assert job.task_name == "integrations.sync_salesforce_opportunity_schema_all_orgs"
    assert job.queue == "integrations"
    assert job.kwargs == {}


def test_salesforce_cdc_subscriber_is_not_fixed_scheduled() -> None:
    with pytest.raises(KeyError):
        get_scheduled_job("run-salesforce-cdc-subscriber")
