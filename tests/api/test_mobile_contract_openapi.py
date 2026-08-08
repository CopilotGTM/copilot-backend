import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from src.api.routers.users.auth_handler import router as auth_router
from src.api.routers.users.signup_handler import router as signup_router
from src.domain.models.user_models import UserRealtimeCallAttachResponse


def test_mobile_auth_routes_publish_bearer_security_in_openapi() -> None:
    app = FastAPI()
    app.include_router(auth_router, prefix="/users/auth")
    app.include_router(signup_router, prefix="/users/auth")

    paths = app.openapi()["paths"]
    expected_security = [{"UserBearerAuth": []}]

    assert paths["/users/auth/{provider}/complete"]["post"]["security"] == expected_security
    assert paths["/users/auth/complete-password-setup"]["post"]["security"] == expected_security
    assert paths["/users/auth/me"]["get"]["security"] == expected_security
    assert paths["/users/auth/logout"]["post"]["security"] == expected_security
    assert paths["/users/auth/{provider}/signup/draft"]["put"]["security"] == expected_security
    assert paths["/users/auth/{provider}/signup/join"]["post"]["security"] == expected_security
    assert paths["/users/auth/{provider}/signup"]["post"]["security"] == expected_security
    assert paths["/users/auth/signup-requests/{signup_request_id}"]["get"]["security"] == (
        expected_security
    )


def test_realtime_attach_status_is_queued_literal() -> None:
    response = UserRealtimeCallAttachResponse(
        call_id="call-123",
        chat_session_id="chat-123",
        control_mode="backend_sideband",
        status="queued",
    )

    assert response.status == "queued"
    assert UserRealtimeCallAttachResponse.model_json_schema()["properties"]["status"]["const"] == (
        "queued"
    )

    with pytest.raises(ValidationError):
        UserRealtimeCallAttachResponse(
            call_id="call-123",
            chat_session_id="chat-123",
            control_mode="backend_sideband",
            status="attached",
        )
