from src.workers.integrations_worker.config import TASK_LIMITS


def test_salesforce_initial_sync_record_phases_allow_large_orgs() -> None:
    for task_name in [
        "integrations.sync_salesforce_companies",
        "integrations.sync_salesforce_contacts",
    ]:
        limits = TASK_LIMITS[task_name]

        assert limits["soft"] == 7200
        assert limits["hard"] == 7260


def test_salesforce_tasks_allow_large_orgs() -> None:
    limits = TASK_LIMITS["integrations.sync_salesforce_tasks"]

    assert limits["soft"] == 7200
    assert limits["hard"] == 7260


def test_salesforce_deals_allows_extra_large_initial_syncs() -> None:
    limits = TASK_LIMITS["integrations.sync_salesforce_deals"]

    assert limits["soft"] == 7200
    assert limits["hard"] == 7260


def test_salesforce_notes_stays_at_one_hour() -> None:
    limits = TASK_LIMITS["integrations.sync_salesforce_notes"]

    assert limits["soft"] == 3600
    assert limits["hard"] == 3660
