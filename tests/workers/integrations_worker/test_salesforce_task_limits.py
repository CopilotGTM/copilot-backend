from src.workers.integrations_worker.config import TASK_LIMITS


def test_salesforce_initial_sync_record_phases_allow_large_orgs() -> None:
    for task_name in [
        "integrations.sync_salesforce_companies",
        "integrations.sync_salesforce_contacts",
        "integrations.sync_salesforce_deals",
        "integrations.sync_salesforce_tasks",
        "integrations.sync_salesforce_notes",
    ]:
        limits = TASK_LIMITS[task_name]

        assert limits["soft"] == 900
        assert limits["hard"] == 960
