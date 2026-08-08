from pathlib import Path

SCRIPT_PATH = Path("scripts/deployment/local-deploy.ps1")


def _full_deploy_helm_args(script_text: str) -> str:
    marker = 'Write-Host "Full deployment mode: reusing existing Helm values'
    start = script_text.index(marker)
    end = script_text.index("    # Execute Helm command and check for errors", start)
    return script_text[start:end]


def test_full_local_deploy_reuses_existing_cluster_values() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    full_deploy_args = _full_deploy_helm_args(script_text)

    existing_release_branch = full_deploy_args[
        full_deploy_args.index("if ($releaseExists) {") : full_deploy_args.index(
            "} else {", full_deploy_args.index("if ($releaseExists) {")
        )
    ]

    assert '"--reuse-values"' in existing_release_branch
    assert '"-f", "$CHART_PATH/values-dev.yaml"' not in existing_release_branch
    assert "api.secrets." not in existing_release_branch
    assert "global.redis.host" not in existing_release_branch
    assert "global.redis.port" not in existing_release_branch


def test_full_local_deploy_applies_async_chat_worker_runtime() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    full_deploy_args = _full_deploy_helm_args(script_text)

    assert '"chat-worker.config.concurrency=10"' in full_deploy_args
    assert '"chat-worker.config.pool=threads"' in full_deploy_args
    assert '"chat-worker.config.maxTasksPerChild=null"' in full_deploy_args
    assert '"chat-worker.config.maxMemoryPerChildKb=null"' in full_deploy_args


def test_full_local_deploy_keeps_bootstrap_install_values() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    full_deploy_args = _full_deploy_helm_args(script_text)

    assert "$releaseExists = $LASTEXITCODE -eq 0" in script_text
    assert "Fresh install mode: using values-dev.yaml and environment bootstrap values" in (
        full_deploy_args
    )
    assert '"-f", "$CHART_PATH/values-dev.yaml"' in full_deploy_args
    assert '"--create-namespace"' in full_deploy_args
    assert "api.secrets.REDIS_HOST=$($env:REDIS_HOST)" in full_deploy_args
    assert "global.redis.host=$($env:REDIS_HOST)" in full_deploy_args
    assert (
        "chat-beat.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)"
        in full_deploy_args
    )
    assert (
        "chat-beat.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)"
        in full_deploy_args
    )
    assert "chat-beat.image.repository=$lightRepo" in full_deploy_args
    assert "chat-beat.image.tag=$IMAGE_TAG" in full_deploy_args
    assert "deal-insights-worker.image.repository=$lightRepo" in full_deploy_args
    assert "deal-insights-worker.image.tag=$IMAGE_TAG" in full_deploy_args
    assert "deal-insights-worker.enabled=true" in full_deploy_args


def test_local_deploy_uses_only_the_master_key_vault() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "AZURE_KEY_VAULT_URL" not in script_text
    assert "AZURE_MASTER_KEY_VAULT_URL" in script_text


def test_full_local_deploy_skips_startup_hooks_by_default_with_opt_in_flag() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    full_deploy_args = _full_deploy_helm_args(script_text)

    assert "[bool]$RunStartupJobs = $false" in script_text
    assert '"--timeout", "20m"' in full_deploy_args
    assert "if (-not $RunStartupJobs) {" in full_deploy_args
    assert '"--no-hooks"' in full_deploy_args


def test_pending_release_rollback_waits_before_next_upgrade() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "helm rollback $HELM_RELEASE_NAME -n $NAMESPACE --wait --timeout 20m --no-hooks" in (
        script_text
    )


def test_full_local_deploy_does_not_force_second_worker_restart() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "kubectl rollout restart deployment $deployment -n $NAMESPACE" not in script_text
    assert "Waiting for API and worker deployments updated by Helm" in script_text
    assert '"copilot-backend",' in script_text
    assert '"copilot-backend-chat-beat",' in script_text
    assert '"copilot-backend-deal-insights-worker"' in script_text
