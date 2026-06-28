from __future__ import annotations

from pathlib import Path


def test_apollo_enrichment_is_exposed_from_service_package() -> None:
    from src.service.enrichment.apollo import apollo_enrich_organization, apollo_enrich_person

    assert callable(apollo_enrich_person)
    assert callable(apollo_enrich_organization)


def test_application_code_imports_apollo_from_service_package() -> None:
    forbidden = "src.agents.tools.research.apollo_client"
    repo_root = Path(__file__).resolve().parents[3]
    offenders: list[str] = []

    removed_client = repo_root / "src" / "agents" / "tools" / "research" / "apollo_client.py"
    assert not removed_client.exists()

    for path in (repo_root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if forbidden in text:
            offenders.append(str(path.relative_to(repo_root)))

    assert offenders == []
