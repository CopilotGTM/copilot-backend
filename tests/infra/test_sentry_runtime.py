from src.infra.observability.sentry import _scrub_event


def test_scrub_event_redacts_sensitive_request_user_and_extra_fields():
    # Cover the major Sentry payload surfaces that can carry tenant/user secrets.
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret-token",
                "Cookie": "session=secret",
                "apikey": "service-role-key",
                "X-Request-Id": "req-123",
            },
            "data": {
                "client_secret": "oauth-client-secret",
                "password": "top-secret",
                "nested": {"refresh_token": "refresh-token"},
                "supabase_service_role_key": "service-role-key",
                "safe": "value",
            },
            "query_string": "api_key=leaked&page=1",
        },
        "user": {"email": "user@example.com", "ip_address": "127.0.0.1"},
        "extra": {"session": "session-id", "safe_extra": "ok"},
        "contexts": {
            "runtime": {"name": "python"},
            "auth": {"token": "context-token"},
        },
        "breadcrumbs": {
            "values": [
                {"message": "login", "data": {"password": "abc", "kept": "yes"}},
                {"message": "no-data"},
            ]
        },
    }

    # before_send returns the same event dict — Sentry treats it as the final payload.
    assert _scrub_event(event, {}) is event
    assert event["request"]["headers"]["Authorization"] == "[Filtered]"
    assert event["request"]["headers"]["Cookie"] == "[Filtered]"
    assert event["request"]["headers"]["apikey"] == "[Filtered]"
    assert event["request"]["headers"]["X-Request-Id"] == "req-123"
    assert event["request"]["data"]["client_secret"] == "[Filtered]"
    assert event["request"]["data"]["password"] == "[Filtered]"
    assert event["request"]["data"]["nested"]["refresh_token"] == "[Filtered]"
    assert event["request"]["data"]["supabase_service_role_key"] == "[Filtered]"
    assert event["request"]["data"]["safe"] == "value"
    assert event["request"]["query_string"] == "[Filtered]"
    assert event["user"]["ip_address"] == "[Filtered]"
    assert event["extra"]["session"] == "[Filtered]"
    assert event["extra"]["safe_extra"] == "ok"
    assert event["contexts"]["auth"]["token"] == "[Filtered]"
    assert event["contexts"]["runtime"]["name"] == "python"
    assert event["breadcrumbs"]["values"][0]["data"]["password"] == "[Filtered]"
    assert event["breadcrumbs"]["values"][0]["data"]["kept"] == "yes"
