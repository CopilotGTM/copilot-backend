from src.workers.deal_worker.config import DealWorkerConfig


def test_deal_update_task_routes_to_deal_jobs_queue():
    config = DealWorkerConfig()

    assert config.task_routes["deal_update.process_job"]["queue"] == "deal_jobs"
