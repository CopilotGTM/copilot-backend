from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from src.api.routers.app_preferences import kai_training_handler as handler
from src.domain.models.master_tables import UserAppRole
from src.infra.auth.user_context import UserIDs
from src.service.orgnisation.kai_training_service import KaiTrainingCooldownError


def _user(role: UserAppRole) -> UserIDs:
    return UserIDs(
        user_id=uuid4(),
        organisation_id=uuid4(),
        user_role=role,
    )


def test_member_cannot_edit_or_start_research() -> None:
    with pytest.raises(HTTPException) as error:
        handler._require_admin(_user(UserAppRole.MEMBER))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_research_cooldown_returns_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CoolingDownService:
        def __init__(self, *_args):
            return None

        async def mark_pending(self):
            raise KaiTrainingCooldownError(datetime.now(UTC) + timedelta(minutes=5))

    monkeypatch.setattr(handler, "KaiTrainingService", CoolingDownService)

    with pytest.raises(HTTPException) as error:
        await handler.research_kai_training(
            user_ids=_user(UserAppRole.ADMIN),
            master_session=object(),  # type: ignore[arg-type]
            _rl_write=None,
        )

    assert error.value.status_code == 429
    assert 0 < int(error.value.headers["Retry-After"]) <= 300
