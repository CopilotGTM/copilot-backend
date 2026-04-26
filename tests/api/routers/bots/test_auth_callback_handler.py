from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from src.api.routers.bots import auth_callback_handler
from src.api.schemas.bot_auth_models import BotAuthCallbackRequest
from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.infra.auth.supabase_user_client import SupabaseAuthError
from src.service.bots.auth_state_service import BotAuthState


def _auth_state() -> BotAuthState:
    return BotAuthState(
        state_token="state-1",
        provider=ExternalIdentityProvider.WHATSAPP,
        external_user_id="15551234567",
        external_tenant_id="phone-123",
        external_user_name="Test User",
        context={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
    )


@pytest.mark.asyncio
async def test_callback_does_not_consume_state_when_access_token_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStateService:
        consume_count = 0

        def peek_state(self, state: str):
            return _auth_state()

        def validate_and_consume_state(self, state: str):
            self.consume_count += 1
            return _auth_state()

    state_service = FakeStateService()
    monkeypatch.setattr(
        auth_callback_handler,
        "get_bot_auth_state_service",
        lambda: state_service,
    )
    monkeypatch.setattr(
        auth_callback_handler.supabase_user_client,
        "verify_token",
        lambda _token: (_ for _ in ()).throw(SupabaseAuthError("Token expired", 401)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth_callback_handler.bot_auth_callback(
            BotAuthCallbackRequest(access_token="expired-token", state="state-1"),
            session=SimpleNamespace(),
            identity_service=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 401
    assert state_service.consume_count == 0
