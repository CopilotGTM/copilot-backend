"""Regression checks for the shared-tenant runtime source gate."""

from __future__ import annotations

from scripts.maintenance.check_tenant_runtime_bypasses import _read_files, find_bypasses


def test_flags_direct_tenant_create_engine() -> None:
    files = {
        "src/bad.py": "create_async_engine('postgresql+asyncpg://org_user:pwd@host/db')",
    }

    findings = find_bypasses(files)

    assert findings == ["src/bad.py: direct tenant engine creation"]


def test_allows_migrations_and_maintenance_explicit_org_schema_sql() -> None:
    files = {
        "migrations/org_migrations/versions/x.py": "SELECT * FROM org_123.table",
        "scripts/maintenance/x.py": "SELECT * FROM org_123.table",
    }

    assert find_bypasses(files) == []


def test_allows_database_provider_engine_creation() -> None:
    files = {
        "src/infra/database/provider_async.py": "create_async_engine(url)",
        "src/infra/database/provider_sync.py": "create_engine(url)",
    }

    assert find_bypasses(files) == []


def test_allows_known_migration_script_engine_creation() -> None:
    files = {
        "scripts/run_master_migrations.py": "create_engine(url)",
        "scripts/run_org_migrations.py": "create_engine(url)",
        "scripts/org_admin_session.py": "create_engine(get_master_db_url())",
        "scripts/maintenance/grant_tenant_runtime_schema_access.py": "create_engine(url)",
        "scripts/cleanup_internal_stakeholders.py": "create_engine(url)",
    }

    assert find_bypasses(files) == []


def test_flags_explicit_org_schema_sql_outside_allowlist() -> None:
    files = {
        "src/service/bad.py": "SELECT * FROM org_123.crm_deals",
    }

    assert find_bypasses(files) == ["src/service/bad.py: explicit org schema reference"]


def test_flags_explicit_org_schema_sql_when_uuid_starts_with_hex_letter() -> None:
    files = {
        "src/service/bad.py": "SELECT * FROM org_a1234567.crm_deals",
    }

    assert find_bypasses(files) == ["src/service/bad.py: explicit org schema reference"]


def test_flags_direct_tenant_session_acquisition_from_api_router() -> None:
    """User routes must obtain tenant sessions through the authenticated dependency."""

    files = {
        "src/api/routers/crm/bad.py": "async with org_async_session_scope(organisation_id): pass",
    }

    assert find_bypasses(files) == [
        "src/api/routers/crm/bad.py: direct tenant-session acquisition from API router"
    ]


def test_allows_reviewed_api_tenant_session_ingress_boundaries() -> None:
    """Only authenticated dependencies and durable ingress mappings may select a tenant."""

    files = {
        "src/api/dependencies/organisation_dependencies.py": (
            "async for session in acquire_org_async_session(user_ids.organisation_id): yield session"
        ),
        "src/api/routers/crm/leads/lead_forms_handler.py": (
            "async for session in acquire_org_async_session(ingress_key.organisation_id): pass"
        ),
        "src/api/routers/crm/leads/private_lead_ingestion_handler.py": (
            "async for session in acquire_org_async_session(ingress_key.organisation_id): pass"
        ),
    }

    assert find_bypasses(files) == []


def test_runtime_source_has_no_tenant_bypasses() -> None:
    """Make the real source scan part of the normal pytest release gate."""

    assert find_bypasses(_read_files(["src"])) == []


def test_checker_is_strict_by_default() -> None:
    from scripts.maintenance.check_tenant_runtime_bypasses import exit_code_for_findings

    assert exit_code_for_findings(["src/service/bad.py: explicit org schema reference"]) == 1
    assert (
        exit_code_for_findings(
            ["src/service/bad.py: explicit org schema reference"],
            advisory=True,
        )
        == 0
    )


def test_release_gate_scans_runtime_src_only_by_default() -> None:
    from scripts.maintenance.check_tenant_runtime_bypasses import _default_scan_roots

    assert _default_scan_roots() == ["src"]
    assert _default_scan_roots(include_scripts=True) == ["src", "scripts"]
    assert _default_scan_roots(include_tests=True) == ["src", "tests"]


def test_include_scripts_skips_local_investigation_scripts() -> None:
    from scripts.maintenance.check_tenant_runtime_bypasses import _should_scan_path

    assert not _should_scan_path("scripts/test_local/investigate_matching_bug.py")
    assert _should_scan_path("scripts/run_org_migrations.py")
