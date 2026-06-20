"""Boundary tests that keep deal service standardization from regressing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from shutil import which

DEAL_MODULE_FILES = [
    "src/service/crm/deals/__init__.py",
    "src/service/crm/deals/contracts.py",
    "src/service/crm/deals/read_service.py",
    "src/service/crm/deals/write_service.py",
    "src/service/crm/deals/custom_field_service.py",
    "src/service/crm/deals/stage_service.py",
    "src/service/crm/deals/lifecycle_service.py",
    "src/service/crm/deals/owner_service.py",
    "src/service/crm/deals/relationship_service.py",
    "src/service/crm/deals/mutation_event_service.py",
]


def test_deal_vertical_module_exists() -> None:
    """Lock the canonical deal service module layout required by the plan."""
    for filename in DEAL_MODULE_FILES:
        assert Path(filename).exists(), filename


def test_deal_read_service_does_not_expose_writes() -> None:
    """DealReadService must stay read-only so callers cannot mutate through reads."""
    source = Path("src/service/crm/deals/read_service.py").read_text()

    assert "async def create_deal" not in source
    assert "async def update_deal" not in source
    assert "async def move_stage" not in source
    assert "CRMDeal(" not in source


def test_new_deal_services_do_not_import_old_internal_writer() -> None:
    """New deal services must own implementation instead of delegating backward."""
    forbidden = (
        "internal.deal_write_service",
        "from src.service.crm.internal.deal_write_service",
    )
    for filename in Path("src/service/crm/deals").glob("*.py"):
        source = filename.read_text()
        assert not any(pattern in source for pattern in forbidden), filename


def test_provider_deal_sync_writers_are_out_of_scope() -> None:
    """Provider sync deal writers are intentionally excluded from this refactor."""
    git_path = which("git")
    assert git_path is not None
    result = subprocess.run(  # noqa: S603 - test invokes git with fixed arguments only.
        [
            git_path,
            "diff",
            "--name-only",
            "--",
            "src/service/integrations/crm/records/hubspot/deals",
            "src/service/integrations/crm/records/salesforce/deals",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == ""
