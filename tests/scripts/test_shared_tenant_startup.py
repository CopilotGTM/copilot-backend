"""Deployment-startup contract for the permanent shared tenant schema."""

from pathlib import Path


def test_startup_migrates_only_the_permanent_tenant_schema() -> None:
    """A deployment must never revisit per-organisation migration targets."""

    startup_text = Path("scripts/startup.sh").read_text(encoding="utf-8")

    assert "python -m scripts.run_org_migrations --schema tenant_data" in startup_text
    assert "python -m scripts.run_org_migrations --all" not in startup_text
