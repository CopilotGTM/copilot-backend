from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_local_env_declares_slack_redirect_uri_for_integration_callback() -> None:
    env_text = (REPO_ROOT / ".env").read_text(encoding="utf-8")

    assert (
        "SLACK_REDIRECT_URI="
        "https://caitlyn-nonarmigerous-uninstructively.ngrok-free.dev/"
        "users/integrations/copilot-oauth/callback"
    ) in env_text


def test_helm_env_overrides_declare_slack_redirect_uri() -> None:
    dev_values = (REPO_ROOT / "charts" / "application" / "values-dev.yaml").read_text(
        encoding="utf-8"
    )
    prod_values = (REPO_ROOT / "charts" / "application" / "values-prod.yaml").read_text(
        encoding="utf-8"
    )

    assert (
        'SLACK_REDIRECT_URI: "https://devapi.copilotgtm.com/'
        'users/integrations/copilot-oauth/callback"'
    ) in dev_values
    assert (
        'SLACK_REDIRECT_URI: "https://api.copilotgtm.com/users/integrations/copilot-oauth/callback"'
    ) in prod_values


def test_slack_app_manifests_declare_integration_redirect_uri() -> None:
    dev_manifest = (
        REPO_ROOT / "infra" / "manifests" / "slack-app" / "dev" / "manifest.yaml"
    ).read_text(encoding="utf-8")
    prod_manifest = (
        REPO_ROOT / "infra" / "manifests" / "slack-app" / "prod" / "manifest.yaml"
    ).read_text(encoding="utf-8")

    assert "https://devapi.copilotgtm.com/users/integrations/copilot-oauth/callback" in dev_manifest
    assert "https://api.copilotgtm.com/users/integrations/copilot-oauth/callback" in prod_manifest


def test_helm_env_overrides_declare_hubspot_and_salesforce_redirect_uris() -> None:
    dev_values = (REPO_ROOT / "charts" / "application" / "values-dev.yaml").read_text(
        encoding="utf-8"
    )
    prod_values = (REPO_ROOT / "charts" / "application" / "values-prod.yaml").read_text(
        encoding="utf-8"
    )

    assert (
        'HUBSPOT_REDIRECT_URI: "https://devapi.copilotgtm.com/'
        'users/integrations/copilot-oauth/callback"'
    ) in dev_values
    assert (
        'SALESFORCE_REDIRECT_URI: "https://devapi.copilotgtm.com/'
        'users/integrations/copilot-oauth/callback"'
    ) in dev_values
    assert (
        'HUBSPOT_REDIRECT_URI: "https://api.copilotgtm.com/'
        'users/integrations/copilot-oauth/callback"'
    ) in prod_values
    assert (
        'SALESFORCE_REDIRECT_URI: "https://api.copilotgtm.com/'
        'users/integrations/copilot-oauth/callback"'
    ) in prod_values
