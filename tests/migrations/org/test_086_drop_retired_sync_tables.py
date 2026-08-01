"""Regression tests for the consolidated retired-sync-table cleanup."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYNC_MODELS = ROOT / "src" / "domain" / "models" / "organisation_tables" / "sync"
VERSIONS = ROOT / "migrations" / "org_migrations" / "versions"
MIGRATION_PATH = VERSIONS / "3c7f79637713_086_drop_retired_sync_tables.py"


def _load_revision() -> Any:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("org_migration_086", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retired_models_and_runtime_wiring_are_removed() -> None:
    """All retired schemas must be absent from runtime registration and tooling."""
    assert not (SYNC_MODELS / "timeline_models.py").exists()
    assert not (SYNC_MODELS / "conversation_intelligence.py").exists()
    assert not (
        ROOT
        / "src"
        / "workers"
        / "integrations_worker"
        / "providers"
        / "messaging"
        / "teams_transcripts.py"
    ).exists()

    for relative_path in (
        "docs/Technical Documentation/database/email-integration-schema.md",
        "docs/Technical Documentation/database/messaging-integration-schema.md",
    ):
        assert not (ROOT / relative_path).exists()

    source_contracts = {
        "src/domain/models/organisation_tables/sync/__init__.py": (
            "DealTimelineEvent",
            "EmailThreadSummary",
            "MessagingThreadSummary",
            "IntegrationParticipantIdentity",
            "CICall",
            "conversation_intelligence",
        ),
        "src/domain/models/organisation_tables/sync/email_models.py": (
            "EmailThreadSummary",
        ),
        "src/domain/models/organisation_tables/sync/messaging_models.py": (
            "MessagingThreadSummary",
        ),
        "src/domain/models/organisation_tables/sync/participant_models.py": (
            "IntegrationParticipantIdentity",
            "integration_participant_identities",
            "CICall",
        ),
        "migrations/org_migrations/env.py": (
            "DealTimelineEvent",
            "EmailThreadSummary",
            "MessagingThreadSummary",
            "IntegrationParticipantIdentity",
            "CICall",
            "conversation_intelligence",
        ),
    }
    for relative_path, retired_names in source_contracts.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        for retired_name in retired_names:
            assert retired_name not in source

    maintenance_contracts = {
        "scripts/maintenance/purge_email_data.py": ("email_thread_summaries",),
        "scripts/maintenance/purge_messaging_channels.py": (
            "messaging_thread_summaries",
        ),
        "scripts/maintenance/purge_messaging_messages.py": (
            "messaging_thread_summaries",
        ),
        "scripts/maintenance/purge_salesforce_initial_sync_data.py": (
            "deal_timeline_events",
            "email_thread_summaries",
            "messaging_thread_summaries",
        ),
        "scripts/maintenance/merge_provider_parent_integrations.py": (
            "deal_timeline_events",
            "email_thread_summaries",
            "messaging_thread_summaries",
        ),
    }
    for relative_path, retired_tables in maintenance_contracts.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        for retired_table in retired_tables:
            assert retired_table not in source


def test_cleanup_is_one_revision_after_forced_rls_head() -> None:
    """The unshipped cleanup must occupy one Alembic revision instead of five."""
    migration = _load_revision()

    assert migration.revision == "3c7f79637713"
    assert migration.down_revision == "3c7f79637714"
    assert not tuple(VERSIONS.glob("*_087_*.py"))
    assert not tuple(VERSIONS.glob("*_088_*.py"))
    assert not tuple(VERSIONS.glob("*_089_*.py"))
    assert not tuple(VERSIONS.glob("*_090_*.py"))


def test_cleanup_drops_ci_children_before_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drop all nine tables once, keeping CI children ahead of their parent."""
    migration = _load_revision()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    migration.upgrade()

    assert calls == [
        (("ci_call_analytics",), {"if_exists": True}),
        (("ci_transcript_segments",), {"if_exists": True}),
        (("ci_call_speakers",), {"if_exists": True}),
        (("ci_call_deal_links",), {"if_exists": True}),
        (("ci_calls",), {"if_exists": True}),
        (("integration_participant_identities",), {"if_exists": True}),
        (("messaging_thread_summaries",), {"if_exists": True}),
        (("email_thread_summaries",), {"if_exists": True}),
        (("deal_timeline_events",), {"if_exists": True}),
    ]
