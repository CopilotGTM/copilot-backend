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


def test_full_local_deploy_skips_startup_hooks_by_default_with_opt_in_flag() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    full_deploy_args = _full_deploy_helm_args(script_text)

    assert "[bool]$RunStartupJobs = $false" in script_text
    assert "if (-not $RunStartupJobs) {" in full_deploy_args
    assert '"--no-hooks"' in full_deploy_args


def test_full_local_deploy_does_not_force_second_worker_restart() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "kubectl rollout restart deployment $deployment -n $NAMESPACE" not in script_text
    assert "Waiting for worker deployments updated by Helm" in script_text
