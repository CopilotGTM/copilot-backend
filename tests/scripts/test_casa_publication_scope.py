from scripts.security import check_casa_publication_scope as scope


def test_publication_scope_passes_when_required_files_exist(tmp_path):
    for relative_path in scope.REQUIRED_PUBLICATION_FILES:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")

    result = scope.check_publication_scope(tmp_path)

    assert result.passed is True
    assert result.failures == []


def test_publication_scope_reports_missing_required_files(tmp_path):
    result = scope.check_publication_scope(tmp_path)

    assert result.passed is False
    assert any(
        ".azure-pipelines/azure-backend-pipeline-prod.yml" in failure for failure in result.failures
    )
    assert any("infra/security/casa/README.md" in failure for failure in result.failures)


def test_publication_scope_report_can_print_file_manifest():
    result = scope.ScopeCheckResult(passed=True, failures=[])

    report = scope.format_report(result, include_files=True)

    assert report.startswith("CASA publication scope check: PASS")
    assert ".azure-pipelines/azure-backend-pipeline-prod.yml" in report
    assert "infra/security/casa/evidence/.gitignore" in report
    assert "scripts/security/check_casa_release_publication.py" in report


def test_publication_scope_report_can_print_git_add_command():
    result = scope.ScopeCheckResult(passed=True, failures=[])

    report = scope.format_report(result, include_git_add=True)

    assert "Git add command:" in report
    assert 'git add -- ".azure-pipelines/azure-backend-pipeline-prod.yml"' in report
    assert '"infra/security/casa/evidence/.gitignore"' in report
    assert '"infra/docs/Technical Documentation/soc2-casa-preparedness.md"' in report
    assert "casa-scope-values.local.json" not in report
    assert "submission-preflight.txt" not in report


def test_publication_scope_report_can_print_release_commit_commands():
    result = scope.ScopeCheckResult(passed=True, failures=[])

    report = scope.format_report(result, include_release_commands=True)

    assert "Release commit commands:" in report
    assert (
        "uv run python scripts/security/check_casa_publication_scope.py --fail-on-non-casa-staged"
        in report
    )
    assert 'git commit -m "Prepare CASA production evidence workflow"' in report


def test_publication_scope_keeps_local_values_and_generated_evidence_out_of_manifest():
    assert (
        "infra/security/casa/casa-scope-values.local.json" not in scope.REQUIRED_PUBLICATION_FILES
    )
    assert (
        "infra/security/casa/evidence/submission-preflight.txt"
        not in scope.REQUIRED_PUBLICATION_FILES
    )
    assert "infra/security/casa/casa-scope-values.local.json" in scope.FORBIDDEN_PUBLICATION_FILES


def test_publication_scope_classifies_changed_files():
    status_lines = [
        " M .azure-pipelines/azure-backend-pipeline-prod.yml",
        "?? infra/security/casa/README.md",
        " M src/service/unrelated.py",
    ]

    changed = scope.classify_changed_files(status_lines)

    assert changed.casa_files == [
        ".azure-pipelines/azure-backend-pipeline-prod.yml",
        "infra/security/casa/README.md",
    ]
    assert changed.non_casa_files == ["src/service/unrelated.py"]


def test_publication_scope_report_can_print_worktree_scope():
    changed = scope.ChangedFileScope(
        casa_files=["infra/security/casa/README.md"],
        non_casa_files=["src/service/unrelated.py"],
    )

    report = scope.format_changed_files_report(changed)

    assert "CASA changed files:" in report
    assert "- infra/security/casa/README.md" in report
    assert "Non-CASA changed files:" in report
    assert "- src/service/unrelated.py" in report


def test_publication_scope_can_fail_when_non_casa_files_changed():
    changed = scope.ChangedFileScope(
        casa_files=["infra/security/casa/README.md"],
        non_casa_files=["src/service/unrelated.py"],
    )

    result = scope.non_casa_change_result(changed)

    assert result.passed is False
    assert result.failures == ["non-CASA changed file present: src/service/unrelated.py"]


def test_publication_scope_can_fail_when_non_casa_files_are_staged():
    changed = scope.ChangedFileScope(
        casa_files=["infra/security/casa/README.md"],
        non_casa_files=["src/service/unrelated.py"],
    )

    result = scope.non_casa_change_result(changed, label="staged")

    assert result.passed is False
    assert result.failures == ["non-CASA staged file present: src/service/unrelated.py"]


def test_publication_scope_uses_name_status_for_staged_files():
    source = scope.Path(scope.__file__).read_text(encoding="utf-8")

    assert '"diff", "--cached", "--name-status"' in source


def test_publication_scope_uses_file_level_untracked_status():
    source = scope.Path(scope.__file__).read_text(encoding="utf-8")

    assert "--untracked-files=all" in source
