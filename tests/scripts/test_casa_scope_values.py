import json

from scripts.security import check_casa_submission_preflight as preflight


def test_google_scope_values_report_missing_required_fields(tmp_path):
    casa_root = tmp_path / "casa"
    casa_root.mkdir()

    missing = preflight.missing_google_scope_values(casa_root)

    assert "google_oauth_consent_app_name" in missing
    assert "assessor_test_account" in missing


def test_google_scope_values_accept_complete_local_values_file(tmp_path):
    casa_root = tmp_path / "casa"
    casa_root.mkdir()
    (casa_root / "casa-scope-values.local.json").write_text(
        json.dumps(
            {
                "google_oauth_consent_app_name": "CopilotGTM",
                "google_oauth_client_id": "client-id.apps.googleusercontent.com",
                "casa_google_notification_email": "casa@example.com",
                "authorized_assessor_path": "Assessor portal",
                "assessor_test_account": "assessor@example.com",
            }
        ),
        encoding="utf-8",
    )

    assert preflight.missing_google_scope_values(casa_root) == []
