from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_helm_env_overrides_declare_explicit_cors_origins() -> None:
    dev_values = (REPO_ROOT / "charts" / "application" / "values-dev.yaml").read_text(
        encoding="utf-8"
    )
    prod_values = (REPO_ROOT / "charts" / "application" / "values-prod.yaml").read_text(
        encoding="utf-8"
    )

    assert (
        'ALLOW_ORIGIN_URL_AUTH: "https://devapp.copilotgtm.com,https://devadmin.copilotgtm.com"'
    ) in dev_values
    assert (
        'ALLOW_ORIGIN_URL_AUTH: "https://app.copilotgtm.com,https://admin.copilotgtm.com"'
    ) in prod_values


def test_helm_base_values_do_not_inline_runtime_signing_secrets() -> None:
    base_values = (REPO_ROOT / "charts" / "application" / "values.yaml").read_text(encoding="utf-8")

    assert "SESSION_SECRET" not in base_values
    assert "SECRET_KEY" not in base_values
    assert "VOICE_REALTIME_CONFIG_SECRET" not in base_values


def test_azure_pipelines_do_not_pass_runtime_signing_secrets_to_helm() -> None:
    dev_pipeline = (REPO_ROOT / ".azure-pipelines" / "azure-pipelines-dev.yml").read_text(
        encoding="utf-8"
    )
    prod_pipeline = (REPO_ROOT / ".azure-pipelines" / "azure-pipelines-prod.yml").read_text(
        encoding="utf-8"
    )

    for pipeline_text in (dev_pipeline, prod_pipeline):
        assert '--set api.secrets.SESSION_SECRET="$(SESSION_SECRET)"' not in pipeline_text
        assert '--set api.secrets.SECRET_KEY="$(SECRET_KEY)"' not in pipeline_text
        assert (
            '--set api.secrets.VOICE_REALTIME_CONFIG_SECRET="$(VOICE_REALTIME_CONFIG_SECRET)"'
            not in pipeline_text
        )
        assert "SESSION_SECRET: $(SESSION_SECRET)" not in pipeline_text
        assert "SECRET_KEY: $(SECRET_KEY)" not in pipeline_text
        assert "VOICE_REALTIME_CONFIG_SECRET: $(VOICE_REALTIME_CONFIG_SECRET)" not in pipeline_text
