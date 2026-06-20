from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_account_vertical_module_exists() -> None:
    assert Path("src/service/crm/accounts/__init__.py").exists()
    assert Path("src/service/crm/accounts/contracts.py").exists()
    assert Path("src/service/crm/accounts/read_service.py").exists()
    assert Path("src/service/crm/accounts/write_service.py").exists()


def test_account_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/accounts/read_service.py").read_text()

    assert "async def create_account" not in source
    assert "async def update_account" not in source
    assert "CRMCompany(" not in source


def test_account_write_service_is_only_crm_service_company_constructor() -> None:
    rg_path = shutil.which("rg")
    assert rg_path is not None

    result = subprocess.run(  # noqa: S603
        [
            rg_path,
            "-n",
            "CRMCompany\\(",
            "src/service/crm/access",
            "src/service/crm/internal/deal_write_service.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    unexpected = [
        line
        for line in result.stdout.splitlines()
        if "src/service/crm/accounts/write_service.py" not in line.replace("\\", "/")
    ]
    assert unexpected == []
