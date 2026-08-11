import subprocess
import sys


def test_worker_entrypoint_import_is_independent_of_celery_and_api() -> None:
    """Keep startup independent from the Celery application and API package graphs."""
    script = """
import sys
import src.workers.salesforce_cdc_worker.main
assert "src.workers.integrations_worker.celery_app" not in sys.modules
assert not any(name == "src.api" or name.startswith("src.api.") for name in sys.modules)
"""

    subprocess.run(  # noqa: S603 - executable and script are process-owned constants
        [sys.executable, "-c", script],
        check=True,
    )
