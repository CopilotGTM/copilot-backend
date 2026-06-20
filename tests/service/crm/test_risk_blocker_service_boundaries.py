from __future__ import annotations

from pathlib import Path


def test_risks_vertical_module_exists() -> None:
    assert Path("src/service/crm/risks/__init__.py").exists()
    assert Path("src/service/crm/risks/contracts.py").exists()
    assert Path("src/service/crm/risks/mapper.py").exists()
    assert Path("src/service/crm/risks/read_service.py").exists()
    assert Path("src/service/crm/risks/write_service.py").exists()
    assert Path("src/service/crm/risks/lifecycle_service.py").exists()
    assert Path("src/service/crm/risks/source_resolver.py").exists()


def test_risk_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/risks/read_service.py").read_text()

    assert "async def create" not in source
    assert "async def update" not in source
    assert "async def dismiss" not in source
    assert "async def resolve" not in source
    assert "DealRiskBlocker(" not in source


def test_risk_write_service_does_not_commit() -> None:
    source = Path("src/service/crm/risks/write_service.py").read_text()
    lifecycle = Path("src/service/crm/risks/lifecycle_service.py").read_text()

    assert ".commit(" not in source
    assert ".commit(" not in lifecycle


def test_legacy_risk_service_is_facade_after_migration() -> None:
    source = Path("src/service/crm/insights/risk_blocker_service.py").read_text()

    assert "RiskBlockerReadService" in source
    assert "RiskBlockerWriteService" in source
    assert "RiskBlockerLifecycleService" in source
