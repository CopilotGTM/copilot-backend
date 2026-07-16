"""Tests for nonce validation in AddDealNoteFormHandler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.service.bots.form_handlers.add_deal_note_handler import AddDealNoteFormHandler


@pytest.fixture
def handler() -> AddDealNoteFormHandler:
    return AddDealNoteFormHandler()


@pytest.fixture
def activity() -> dict:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


class TestAddDealNoteHandlerNonce:
    """Tests for nonce validation."""

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.add_deal_note_handler.validate_and_consume_nonce")
    async def test_rejects_expired_nonce(
        self,
        mock_validate: MagicMock,
        handler: AddDealNoteFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = False

        result = await handler.handle(
            activity,
            {
                "nonce": "expired-nonce",
                "note_content": "Some note",
                "deal_id": "22222222-2222-2222-2222-222222222222",
                "organisation_id": "11111111-1111-1111-1111-111111111111",
            },
        )

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_called_once_with("note_confirm", "expired-nonce")

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.add_deal_note_handler.validate_and_consume_nonce")
    async def test_rejects_missing_nonce(
        self,
        mock_validate: MagicMock,
        handler: AddDealNoteFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(
            activity,
            {
                "note_content": "Some note",
                "deal_id": "22222222-2222-2222-2222-222222222222",
            },
        )

        assert result.success is False

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.add_deal_note_handler.validate_and_consume_nonce")
    async def test_success_includes_note_and_deal_ids(
        self,
        mock_validate: MagicMock,
        handler: AddDealNoteFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = True
        organisation_id = UUID("11111111-1111-1111-1111-111111111111")
        deal_id = UUID("22222222-2222-2222-2222-222222222222")
        note_id = UUID("33333333-3333-3333-3333-333333333333")
        user = SimpleNamespace(
            organisation_id=organisation_id,
            user_id=UUID("44444444-4444-4444-4444-444444444444"),
            first_name="Nikhil",
            last_name="GTM",
            email="nikhil@example.com",
        )
        org_session = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
        notes_service = MagicMock()
        notes_service.create_user_note = AsyncMock(
            return_value=SimpleNamespace(
                created_new=True,
                note=SimpleNamespace(activity_id=note_id),
            )
        )

        async def acquire_org_session(_organisation_id: UUID):
            yield org_session

        with (
            patch(
                "src.service.bots.form_handlers.add_deal_note_handler.resolve_user_from_activity",
                new=AsyncMock(return_value=(user, None)),
            ),
            patch(
                "src.infra.database.sessions.acquire_org_async_session",
                new=acquire_org_session,
            ),
            patch(
                "src.service.crm.notes.write_service.NotesWriteService",
                return_value=notes_service,
            ),
        ):
            result = await handler.handle(
                activity,
                {
                    "nonce": "valid-nonce",
                    "note_content": "Customer approved the proposal",
                    "deal_id": str(deal_id),
                    "deal_name": "Acme Deal",
                    "organisation_id": str(organisation_id),
                },
            )

        assert result.success is True
        assert str(note_id) in result.message
        assert str(deal_id) in result.message
