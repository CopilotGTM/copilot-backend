from pathlib import Path

PIPELINE_PATHS = [
    Path(".azure-pipelines/azure-backend-pipeline-dev.yml"),
    Path(".azure-pipelines/azure-backend-pipeline-prod.yml"),
]
CHART_VALUES_PATH = Path("charts/application/values.yaml")
STARTUP_SCRIPT_PATH = Path("scripts/startup.sh")
PROD_PIPELINE_PATH = Path(".azure-pipelines/azure-backend-pipeline-prod.yml")
LOCAL_DEPLOY_PATH = Path("scripts/deployment/local-deploy.ps1")
PYPROJECT_PATH = Path("pyproject.toml")


def test_backend_pipelines_do_not_force_second_worker_rollout() -> None:
    for pipeline_path in PIPELINE_PATHS:
        pipeline_text = pipeline_path.read_text(encoding="utf-8")

        assert "kubectl rollout restart deployment" not in pipeline_text, pipeline_path
        assert "Force restart workers" not in pipeline_text, pipeline_path
        assert "--timeout 20m" in pipeline_text, pipeline_path
        assert "Waiting for API and worker deployments updated by Helm" in (pipeline_text), (
            pipeline_path
        )
        assert "wait_if_present copilot-backend\n" in pipeline_text, pipeline_path


def test_backend_pipelines_do_not_prune_local_docker_cache_before_build() -> None:
    for pipeline_path in PIPELINE_PATHS:
        pipeline_text = pipeline_path.read_text(encoding="utf-8")

        assert "Clean Up Disk Space & Check" not in pipeline_text, pipeline_path
        assert "docker builder prune" not in pipeline_text, pipeline_path


def test_backend_pipeline_rollback_uses_release_namespace() -> None:
    for pipeline_path in PIPELINE_PATHS:
        pipeline_text = pipeline_path.read_text(encoding="utf-8")

        assert "helm status $(HELM_RELEASE_NAME) -n copilotgtm" in pipeline_text, pipeline_path
        assert "helm rollback $(HELM_RELEASE_NAME) -n copilotgtm --wait --timeout 20m" in (
            pipeline_text
        ), pipeline_path
        assert "helm history $(HELM_RELEASE_NAME)" not in pipeline_text, pipeline_path
        assert "$REVISION" not in pipeline_text, pipeline_path
        assert "helm status $(HELM_RELEASE_NAME);" not in pipeline_text, pipeline_path
        assert "export KUBECONFIG=$(KUBECONFIG_FILE)" not in pipeline_text, pipeline_path
        assert "KUBECONFIG_FILE" not in pipeline_text, pipeline_path


def test_backend_pipelines_deploy_and_wait_for_deal_insights_worker() -> None:
    for pipeline_path in PIPELINE_PATHS:
        pipeline_text = pipeline_path.read_text(encoding="utf-8")

        assert "--set deal-insights-worker.image.repository=" in pipeline_text, pipeline_path
        assert "--set deal-insights-worker.image.tag=" in pipeline_text, pipeline_path
        assert "--set deal-insights-worker.enabled=true" in pipeline_text, pipeline_path
        assert "wait_if_present copilot-backend-deal-insights-worker" in pipeline_text, (
            pipeline_path
        )


def test_deploy_paths_reuse_integrations_image_for_shared_salesforce_worker() -> None:
    for pipeline_path in PIPELINE_PATHS:
        pipeline_text = pipeline_path.read_text(encoding="utf-8")

        assert (
            '--set salesforce-cdc-worker.image.repository="$ACR_LOGIN_SERVER/$(INTEGRATIONS_IMAGE_REPOSITORY)"'
            in pipeline_text
        ), pipeline_path
        assert '--set salesforce-cdc-worker.image.tag="$BUILD_BUILDID"' in pipeline_text, (
            pipeline_path
        )
        assert (
            '--set salesforce-cdc-worker.workloadIdentity.clientId="$(WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)"'
            in pipeline_text
        ), pipeline_path
        assert (
            '--set salesforce-cdc-worker.workloadIdentity.tenantId="$(WORKLOAD_IDENTITY_WORKERS_TENANT_ID)"'
            in pipeline_text
        ), pipeline_path
        assert "wait_if_present copilot-backend-salesforce-cdc-worker" in pipeline_text, (
            pipeline_path
        )
        assert "grep '^pod/copilot-backend-salesforce-cdc-worker-'" in pipeline_text, pipeline_path

    local_deploy_text = LOCAL_DEPLOY_PATH.read_text(encoding="utf-8")
    assert "salesforce-cdc-worker.image.repository=$integrationsRepo" in local_deploy_text
    assert "salesforce-cdc-worker.image.tag=$IMAGE_TAG" in local_deploy_text
    assert "salesforce-cdc-worker.workloadIdentity.clientId=" in local_deploy_text
    assert "salesforce-cdc-worker.workloadIdentity.tenantId=" in local_deploy_text
    assert '"copilot-backend-salesforce-cdc-worker"' in local_deploy_text


def test_backend_pipelines_use_only_the_master_key_vault_url() -> None:
    for pipeline_path in PIPELINE_PATHS:
        pipeline_text = pipeline_path.read_text(encoding="utf-8")

        assert "AZURE_KEY_VAULT_URL" not in pipeline_text, pipeline_path
        assert "AZURE_MASTER_KEY_VAULT_URL" in pipeline_text, pipeline_path


def test_backend_pipelines_capture_chat_worker_rollout_diagnostics() -> None:
    for pipeline_path in PIPELINE_PATHS:
        pipeline_text = pipeline_path.read_text(encoding="utf-8")

        assert "copilot-backend-chat-worker) timeout_seconds=600" in pipeline_text, pipeline_path
        assert "kubectl get pods -n copilotgtm -l app.kubernetes.io/name=worker" in (
            pipeline_text
        ), pipeline_path
        assert "grep '^pod/copilot-backend-chat-worker-'" in pipeline_text, pipeline_path
        assert 'kubectl describe pod -n copilotgtm "$pod"' in pipeline_text, pipeline_path
        assert 'kubectl logs -n copilotgtm "$pod" --all-containers' in pipeline_text, pipeline_path


def test_startup_migration_hook_runs_before_upgrade_rollout() -> None:
    values_text = CHART_VALUES_PATH.read_text(encoding="utf-8")

    assert 'hook: "post-install,pre-upgrade"' in values_text
    assert 'hook: "post-install,post-upgrade"' not in values_text


def test_startup_script_does_not_run_master_migrations_twice() -> None:
    startup_text = STARTUP_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "python -m scripts.run_master_migrations" in startup_text
    assert "python -m scripts.admin.init_super_admin" in startup_text
    assert "python -m scripts.admin.deploy_with_super_admin" not in startup_text


def test_prod_pipeline_exports_detect_secrets_baseline_gate() -> None:
    pipeline_text = PROD_PIPELINE_PATH.read_text(encoding="utf-8")

    assert "check_detect_secrets_baseline.py" in pipeline_text
    assert "artifact: 'secrets-scan-reports'" in pipeline_text
    assert "--baseline .secrets.baseline" in pipeline_text


def test_prod_pipeline_exports_bandit_plain_text_security_report() -> None:
    pipeline_text = PROD_PIPELINE_PATH.read_text(encoding="utf-8")

    assert "security-reports/bandit-report.json" in pipeline_text
    assert "security-reports/bandit-report.txt" in pipeline_text
    assert "targetPath: 'security-reports'" in pipeline_text
    assert "artifact: 'security-report'" in pipeline_text


def test_prod_pipeline_prints_pip_audit_report_before_failing() -> None:
    pipeline_text = PROD_PIPELINE_PATH.read_text(encoding="utf-8")

    assert "audit_status=0" in pipeline_text
    assert "|| audit_status=$?" in pipeline_text
    assert pipeline_text.index("cat sca-reports/pip-audit-report.txt") < pipeline_text.index(
        'exit "$audit_status"'
    )


def test_pypdf_minimum_version_contains_current_security_fixes() -> None:
    pyproject_text = PYPROJECT_PATH.read_text(encoding="utf-8")

    assert "pypdf>=6.13.3" not in pyproject_text
    assert pyproject_text.count("pypdf>=6.15.0,<6.16.0") == 4
