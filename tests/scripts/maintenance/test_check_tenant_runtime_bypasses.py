from __future__ import annotations

from scripts.maintenance.check_tenant_runtime_bypasses import find_bypasses


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
