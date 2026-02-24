# Confirmation Popup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace form-based interactive cards with read-only confirmation preview cards for create_deal and add_deal_note tools, using Redis nonces for duplicate protection.

**Architecture:** Agent tools build read-only Adaptive Cards (FactSet previews) instead of FormDefinition input cards. All field values are embedded in the Confirm button's `data` payload. A shared Redis nonce utility prevents stale/duplicate card submissions. Existing form handlers gain a nonce check at the top; everything else stays the same.

**Tech Stack:** Python 3.11, FastAPI, Redis (via `get_redis_client()`), Teams Adaptive Cards v1.4

---

### Task 1: Create Nonce Utility

**Files:**
- Create: `src/service/bots/form_handlers/nonce.py`
- Test: `tests/service/bots/form_handlers/test_nonce.py`

**Step 1: Write the failing test**

```python
"""Tests for confirmation card nonce utility."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.service.bots.form_handlers.nonce import create_nonce, validate_and_consume_nonce


class TestCreateNonce:
    """Tests for create_nonce."""

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_creates_nonce_and_stores_in_redis(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_get_redis.return_value = mock_client

        nonce = create_nonce("deal_confirm")

        assert isinstance(nonce, str)
        assert len(nonce) == 36  # UUID4 format
        mock_client.set.assert_called_once()
        call_args = mock_client.set.call_args
        assert call_args[0][0] == f"deal_confirm:{nonce}"
        assert call_args[0][1] == "1"
        assert call_args[1]["ex"] == 900

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_custom_ttl(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_get_redis.return_value = mock_client

        nonce = create_nonce("deal_confirm", ttl=300)

        call_args = mock_client.set.call_args
        assert call_args[1]["ex"] == 300


class TestValidateAndConsumeNonce:
    """Tests for validate_and_consume_nonce."""

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_valid_nonce_consumed(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.delete.return_value = 1  # Key existed and was deleted
        mock_get_redis.return_value = mock_client

        result = validate_and_consume_nonce("deal_confirm", "abc-123")

        assert result is True
        mock_client.delete.assert_called_once_with("deal_confirm:abc-123")

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_expired_nonce_rejected(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.delete.return_value = 0  # Key did not exist
        mock_get_redis.return_value = mock_client

        result = validate_and_consume_nonce("deal_confirm", "expired-123")

        assert result is False

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_redis_error_returns_false(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.delete.side_effect = Exception("Redis down")
        mock_get_redis.return_value = mock_client

        result = validate_and_consume_nonce("deal_confirm", "abc-123")

        assert result is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/bots/form_handlers/test_nonce.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.service.bots.form_handlers.nonce'`

**Step 3: Write minimal implementation**

```python
"""Redis-based nonce utility for one-time-use confirmation cards.

Generates a UUID4 nonce, stores it in Redis with a TTL, and provides
atomic validate-and-consume via DELETE (returns 1 if key existed).

Used by create_deal and add_deal_note tools to prevent duplicate
submissions from stale Adaptive Cards.
"""

from __future__ import annotations

import logging
import uuid

from src.infra.messaging.redis_client import get_redis_client

logger = logging.getLogger(__name__)


def create_nonce(prefix: str, ttl: int = 900) -> str:
    """Generate a UUID4 nonce and store it in Redis.

    Args:
        prefix: Key prefix (e.g. "deal_confirm", "note_confirm")
        ttl: Time-to-live in seconds (default 15 minutes)

    Returns:
        The nonce string (UUID4)
    """
    nonce = str(uuid.uuid4())
    key = f"{prefix}:{nonce}"
    client = get_redis_client()
    client.set(key, "1", ex=ttl)
    logger.debug("Created nonce %s (ttl=%ds)", key, ttl)
    return nonce


def validate_and_consume_nonce(prefix: str, nonce: str) -> bool:
    """Atomically validate and consume a nonce (one-time use).

    Uses DELETE which returns 1 if the key existed (valid + consumed)
    or 0 if it didn't (expired/already consumed).

    Args:
        prefix: Same prefix used in create_nonce
        nonce: The nonce string to validate

    Returns:
        True if nonce was valid and consumed, False otherwise
    """
    key = f"{prefix}:{nonce}"
    try:
        client = get_redis_client()
        deleted = client.delete(key)
        if deleted:
            logger.debug("Consumed nonce %s", key)
            return True
        logger.info("Nonce rejected (expired or already used): %s", key)
        return False
    except Exception:
        logger.warning("Redis error validating nonce %s", key, exc_info=True)
        return False
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/service/bots/form_handlers/test_nonce.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add src/service/bots/form_handlers/nonce.py tests/service/bots/form_handlers/test_nonce.py
git commit -m "feat: add Redis nonce utility for confirmation cards"
```

---

### Task 2: Convert create_deal Tool to Confirmation Card

**Files:**
- Modify: `src/agents/tools/crm/create_deal.py`
- Test: `tests/agents/tools/crm/test_create_deal_confirmation.py`

**Context:** The current `create_deal_form()` function builds a `FormDefinition` with input fields. We replace it with a function that builds a read-only preview Adaptive Card with all values embedded in the action button data.

**Step 1: Write the failing test**

```python
"""Tests for create_deal confirmation card (preview mode)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from src.agents.tools.crm.create_deal import create_deal_form


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    ctx.channel = "teams"
    ctx.metadata = {
        "service_url": "https://smba.trafficmanager.net/teams/",
        "conversation_id": "conv-123",
    }

    # Mock acquire_session as async context manager
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.acquire_session = MagicMock(return_value=async_cm)

    return ctx


class TestCreateDealConfirmation:
    """Tests for confirmation card behavior."""

    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="test-nonce-123")
    async def test_sends_preview_card_not_form(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await create_deal_form(
            context=mock_context,
            name="Acme Corp Deal",
            amount=50000,
            currency="USD",
        )

        assert result is not None
        assert result["status"] == "preview_sent"

        # Verify the card was sent
        mock_bot.send_proactive_payload.assert_called_once()
        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Should have FactSet (read-only), not Input fields
        body_types = [item["type"] for item in card_content["body"]]
        assert "FactSet" in body_types
        assert not any("Input" in t for t in body_types)

        # Confirm button should carry deal data + nonce
        confirm_action = card_content["actions"][0]
        assert confirm_action["verb"] == "create_deal_submit"
        assert confirm_action["data"]["name"] == "Acme Corp Deal"
        assert confirm_action["data"]["amount"] == 50000
        assert confirm_action["data"]["nonce"] == "test-nonce-123"

    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="test-nonce-456")
    async def test_empty_fields_shown_as_dash(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await create_deal_form(
            context=mock_context,
            name="Minimal Deal",
        )

        assert result is not None
        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Find the FactSet
        fact_set = next(item for item in card_content["body"] if item["type"] == "FactSet")
        facts_dict = {f["title"]: f["value"] for f in fact_set["facts"]}

        assert facts_dict["Name"] == "Minimal Deal"
        assert facts_dict["Amount"] == "\u2014"  # em dash for empty

    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="nonce-789")
    async def test_cancel_button_has_nonce(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        await create_deal_form(context=mock_context, name="Test")

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        cancel_action = card_content["actions"][1]
        assert cancel_action["verb"] == "create_deal_cancel"
        assert cancel_action["data"]["nonce"] == "nonce-789"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/agents/tools/crm/test_create_deal_confirmation.py -v`
Expected: FAIL (current code returns `form_sent` status, not `preview_sent`)

**Step 3: Rewrite create_deal_form to build confirmation card**

Replace the entire `create_deal_form` function body in `src/agents/tools/crm/create_deal.py`. Keep the function signature identical. Key changes:

1. Remove `FormDefinition`/`FormField`/`get_card_builder` imports, add `create_nonce` import and `TeamsBotService` top-level import
2. After querying pipelines/stages (keep that logic), build a `_build_deal_preview_card()` helper
3. The helper builds a FactSet card with all values, using "\u2014" for empty fields
4. Generate nonce via `create_nonce("deal_confirm")`
5. Embed all field values + nonce + org_id in Confirm button `data`
6. Return `{"status": "preview_sent", ...}` instead of `{"status": "form_sent", ...}`

The full replacement for `create_deal_form`:

```python
async def create_deal_form(
    *,
    context: AgentContext,
    name: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
    description: str | None = None,
    close_date: str | None = None,
    priority: str | None = None,
    account_name: str | None = None,
    account_domain: str | None = None,
) -> dict[str, Any] | None:
    """Build and send a confirmation preview card for deal creation.

    Queries pipelines/stages for defaults, builds a read-only Adaptive Card,
    and sends it to the user's chat. The user clicks Confirm to create the deal.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("create_deal_form: missing organisation_id in context")
        return None

    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")

    if not service_url or not conversation_id:
        logger.warning(
            "create_deal_form: missing service_url or conversation_id in context metadata"
        )
        return {
            "status": "error",
            "message": (
                "Cannot send confirmation card: missing channel context. "
                "This tool is only available in Teams chat."
            ),
        }

    # Query pipelines and stages to resolve default names for the preview
    default_pipeline_name: str | None = None
    default_pipeline_id: str | None = None
    default_stage_name: str | None = None

    pipeline_c = cast(Any, CRMPipeline).__table__.c
    stage_c = cast(Any, CRMDealStage).__table__.c

    async with context.acquire_session() as session:
        try:
            pipeline_stmt = (
                select(
                    col(CRMPipeline.pipeline_id),
                    col(CRMPipeline.name),
                    col(CRMPipeline.is_default),
                )
                .where(
                    pipeline_c.organisation_id == organisation_id,
                    pipeline_c.is_deleted.is_(False),
                    pipeline_c.is_active.is_(True),
                )
                .order_by(
                    pipeline_c.display_order.asc().nulls_last(),
                    pipeline_c.name.asc(),
                )
            )
            pipeline_result = await session.execute(pipeline_stmt)
            pipeline_rows = pipeline_result.all()

            for p in pipeline_rows:
                if p.is_default or default_pipeline_id is None:
                    default_pipeline_id = str(p.pipeline_id)
                    default_pipeline_name = p.name

            # Get first stage of default pipeline for preview display
            if default_pipeline_id:
                stage_stmt = (
                    select(
                        col(CRMDealStage.stage_id),
                        col(CRMDealStage.name),
                    )
                    .where(
                        stage_c.pipeline_id == UUID(default_pipeline_id),
                        stage_c.is_deleted.is_(False),
                    )
                    .order_by(
                        stage_c.display_order.asc().nulls_last(),
                        stage_c.name.asc(),
                    )
                    .limit(1)
                )
                stage_result = await session.execute(stage_stmt)
                first_stage = stage_result.first()
                if first_stage:
                    default_stage_name = first_stage.name
        except Exception as e:
            logger.exception("create_deal_form: error querying pipelines/stages: %s", e)

    # Build preview card
    nonce = create_nonce("deal_confirm")
    em = "\u2014"  # em dash for empty fields

    amount_display = em
    if amount is not None:
        curr = (currency or "USD").upper()
        amount_display = f"{curr} {amount:,.2f}"

    facts = [
        {"title": "Name", "value": name or em},
        {"title": "Amount", "value": amount_display},
        {"title": "Pipeline", "value": default_pipeline_name or em},
        {"title": "Stage", "value": default_stage_name or em},
        {"title": "Close Date", "value": close_date or em},
        {"title": "Priority", "value": (priority or em).capitalize() if priority else em},
        {"title": "Account", "value": account_name or em},
        {"title": "Description", "value": description or em},
    ]

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Create New Deal",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
        {
            "type": "TextBlock",
            "text": "Please confirm or cancel this deal creation.",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        },
    ]

    # All deal data embedded in Confirm button
    confirm_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
        "name": name or "",
        "amount": amount,
        "currency": (currency or "USD").upper(),
        "description": description or "",
        "close_date": close_date or "",
        "priority": priority or "",
        "account_name": account_name or "",
        "account_domain": account_domain or "",
        "pipeline_id": default_pipeline_id or "",
    }

    cancel_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
    }

    confirm_fallback = {"action": "create_deal_submit", **confirm_data}
    cancel_fallback = {"action": "create_deal_cancel", **cancel_data}

    actions = [
        {
            "type": "Action.Execute",
            "title": "Confirm",
            "style": "positive",
            "verb": "create_deal_submit",
            "data": confirm_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Confirm",
                "style": "positive",
                "data": confirm_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Cancel",
            "style": "destructive",
            "verb": "create_deal_cancel",
            "data": cancel_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Cancel",
                "style": "destructive",
                "data": cancel_fallback,
            },
        },
    ]

    card_payload = {
        "text": " ",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                    "actions": actions,
                },
            }
        ],
    }

    # Send the card
    bot_service = TeamsBotService()
    sent = await bot_service.send_proactive_payload(
        service_url=service_url,
        conversation_id=conversation_id,
        payload=card_payload,
        text_fallback="Deal creation preview",
    )

    if sent:
        return {
            "status": "preview_sent",
            "message": (
                "I've sent a deal creation preview to your chat. "
                "Please review the details and click **Confirm** to create it, "
                "or tell me what to change."
            ),
        }
    else:
        logger.error("create_deal_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the deal creation preview. Please try again.",
        }
```

Update imports at the top of the file — remove `FormDefinition`, `FormField`, `get_card_builder`. Add:

```python
from uuid import UUID

from src.service.bots.form_handlers.nonce import create_nonce
from src.service.bots.teams_bot_service import TeamsBotService
```

Also remove the late import of `TeamsBotService` inside the function since it's now a top-level import.

Update the docstring of `create_deal_tool` (the inner function in the factory) to reflect confirmation behavior:

```python
        """Send a confirmation preview card for deal creation.

        Use this tool when the user wants to create a new deal. Extract any deal
        details mentioned in the conversation and pass them as pre-filled values.
        A read-only preview card will be sent for the user to confirm or cancel.

        If the user wants to change any values after seeing the preview, call this
        tool again with the updated values to send a new preview card.
        ...
        """
```

Update the return message check in the factory from `"form_sent"` to `"preview_sent"`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/agents/tools/crm/test_create_deal_confirmation.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add src/agents/tools/crm/create_deal.py tests/agents/tools/crm/test_create_deal_confirmation.py
git commit -m "feat: convert create_deal tool from form to confirmation card"
```

---

### Task 3: Convert add_deal_note Tool to Confirmation Card

**Files:**
- Modify: `src/agents/tools/crm/add_deal_note.py`
- Test: `tests/agents/tools/crm/test_add_deal_note_confirmation.py`

**Context:** Same pattern as Task 2 but simpler — only deal_name and note_content to display.

**Step 1: Write the failing test**

```python
"""Tests for add_deal_note confirmation card (preview mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from src.agents.tools.crm.add_deal_note import add_deal_note_form


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    ctx.channel = "teams"
    ctx.metadata = {
        "service_url": "https://smba.trafficmanager.net/teams/",
        "conversation_id": "conv-123",
    }
    return ctx


class TestAddDealNoteConfirmation:
    """Tests for confirmation card behavior."""

    @patch("src.agents.tools.crm.add_deal_note.TeamsBotService")
    @patch("src.agents.tools.crm.add_deal_note.create_nonce", return_value="note-nonce-123")
    async def test_sends_preview_card(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="Follow up on pricing discussion",
        )

        assert result is not None
        assert result["status"] == "preview_sent"

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Should have FactSet, not Input fields
        body_types = [item["type"] for item in card_content["body"]]
        assert "FactSet" in body_types

        # Confirm button carries note data + nonce
        confirm_action = card_content["actions"][0]
        assert confirm_action["data"]["deal_id"] == "22222222-2222-2222-2222-222222222222"
        assert confirm_action["data"]["note_content"] == "Follow up on pricing discussion"
        assert confirm_action["data"]["nonce"] == "note-nonce-123"

    @patch("src.agents.tools.crm.add_deal_note.TeamsBotService")
    @patch("src.agents.tools.crm.add_deal_note.create_nonce", return_value="nonce-456")
    async def test_cancel_button_has_nonce(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="Test note",
        )

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        cancel_action = payload["attachments"][0]["content"]["actions"][1]
        assert cancel_action["verb"] == "add_deal_note_cancel"
        assert cancel_action["data"]["nonce"] == "nonce-456"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/agents/tools/crm/test_add_deal_note_confirmation.py -v`
Expected: FAIL (current code returns `form_sent`, not `preview_sent`)

**Step 3: Rewrite add_deal_note_form**

Replace the function body in `src/agents/tools/crm/add_deal_note.py`. Keep the same signature.

Remove `FormDefinition`, `FormField`, `get_card_builder` imports. Add:

```python
from src.service.bots.form_handlers.nonce import create_nonce
from src.service.bots.teams_bot_service import TeamsBotService
```

New function body:

```python
async def add_deal_note_form(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    note_content: str | None = None,
) -> dict[str, Any] | None:
    """Build and send a confirmation preview card for adding a deal note."""
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("add_deal_note_form: missing organisation_id in context")
        return None

    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")

    if not service_url or not conversation_id:
        logger.warning(
            "add_deal_note_form: missing service_url or conversation_id in context metadata"
        )
        return {
            "status": "error",
            "message": (
                "Cannot send confirmation card: missing channel context. "
                "This tool is only available in Teams chat."
            ),
        }

    nonce = create_nonce("note_confirm")
    em = "\u2014"

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Add Note to Deal",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Deal", "value": deal_name},
            ],
        },
        {
            "type": "TextBlock",
            "text": f"**Note:** {note_content or em}",
            "wrap": True,
            "spacing": "Small",
        },
        {
            "type": "TextBlock",
            "text": "Please confirm or cancel.",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        },
    ]

    confirm_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
        "deal_id": deal_id,
        "deal_name": deal_name,
        "note_content": note_content or "",
    }

    cancel_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
    }

    confirm_fallback = {"action": "add_deal_note_submit", **confirm_data}
    cancel_fallback = {"action": "add_deal_note_cancel", **cancel_data}

    actions = [
        {
            "type": "Action.Execute",
            "title": "Confirm",
            "style": "positive",
            "verb": "add_deal_note_submit",
            "data": confirm_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Confirm",
                "style": "positive",
                "data": confirm_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Cancel",
            "style": "destructive",
            "verb": "add_deal_note_cancel",
            "data": cancel_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Cancel",
                "style": "destructive",
                "data": cancel_fallback,
            },
        },
    ]

    card_payload = {
        "text": " ",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                    "actions": actions,
                },
            }
        ],
    }

    bot_service = TeamsBotService()
    sent = await bot_service.send_proactive_payload(
        service_url=service_url,
        conversation_id=conversation_id,
        payload=card_payload,
        text_fallback=f"Note preview for deal {deal_name}",
    )

    if sent:
        return {
            "status": "preview_sent",
            "message": (
                f"I've sent a preview to add a note to deal **{deal_name}**. "
                "Please review and click **Confirm**, or tell me what to change."
            ),
        }
    else:
        logger.error("add_deal_note_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the note preview. Please try again.",
        }
```

Update the factory function's docstring similarly (s/form/confirmation preview/).

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/agents/tools/crm/test_add_deal_note_confirmation.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add src/agents/tools/crm/add_deal_note.py tests/agents/tools/crm/test_add_deal_note_confirmation.py
git commit -m "feat: convert add_deal_note tool from form to confirmation card"
```

---

### Task 4: Add Nonce Validation to CreateDealFormHandler

**Files:**
- Modify: `src/service/bots/form_handlers/create_deal_handler.py`
- Test: `tests/service/bots/form_handlers/test_create_deal_handler_nonce.py`

**Context:** The handler currently extracts form fields from `form_data`. The data keys stay the same — they just come from the button's `data` payload now instead of Input fields. We add nonce validation at the top.

**Step 1: Write the failing test**

```python
"""Tests for nonce validation in CreateDealFormHandler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.service.bots.form_handlers.create_deal_handler import CreateDealFormHandler


@pytest.fixture
def handler() -> CreateDealFormHandler:
    return CreateDealFormHandler()


@pytest.fixture
def activity() -> dict:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


class TestCreateDealHandlerNonce:
    """Tests for nonce validation."""

    @patch("src.service.bots.form_handlers.create_deal_handler.validate_and_consume_nonce")
    async def test_rejects_expired_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = False

        result = await handler.handle(activity, {
            "nonce": "expired-nonce",
            "name": "Acme Deal",
            "organisation_id": "11111111-1111-1111-1111-111111111111",
        })

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_called_once_with("deal_confirm", "expired-nonce")

    @patch("src.service.bots.form_handlers.create_deal_handler.validate_and_consume_nonce")
    async def test_rejects_missing_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(activity, {
            "name": "Acme Deal",
            "organisation_id": "11111111-1111-1111-1111-111111111111",
        })

        assert result.success is False
        assert "expired" in result.message.lower() or "invalid" in result.message.lower()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/bots/form_handlers/test_create_deal_handler_nonce.py -v`
Expected: FAIL (no nonce validation exists yet)

**Step 3: Add nonce validation to handler**

At the top of `CreateDealFormHandler.handle()`, before extracting form fields, add:

```python
from src.service.bots.form_handlers.nonce import validate_and_consume_nonce
```

(Add as a top-level import in the file.)

Then at the start of `handle()`:

```python
    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        # Validate one-time nonce to prevent duplicate/stale card submissions
        nonce = form_data.get("nonce", "")
        if not nonce or not validate_and_consume_nonce("deal_confirm", nonce):
            return FormHandlerResult(
                success=False,
                message=(
                    "This confirmation has expired or was already used. "
                    "Please ask the assistant to send a new one."
                ),
            )

        # ... rest of existing handler code unchanged ...
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/service/bots/form_handlers/test_create_deal_handler_nonce.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add src/service/bots/form_handlers/create_deal_handler.py tests/service/bots/form_handlers/test_create_deal_handler_nonce.py
git commit -m "feat: add nonce validation to create deal handler"
```

---

### Task 5: Add Nonce Validation to AddDealNoteFormHandler

**Files:**
- Modify: `src/service/bots/form_handlers/add_deal_note_handler.py`
- Test: `tests/service/bots/form_handlers/test_add_deal_note_handler_nonce.py`

**Step 1: Write the failing test**

```python
"""Tests for nonce validation in AddDealNoteFormHandler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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

    @patch("src.service.bots.form_handlers.add_deal_note_handler.validate_and_consume_nonce")
    async def test_rejects_expired_nonce(
        self,
        mock_validate: MagicMock,
        handler: AddDealNoteFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = False

        result = await handler.handle(activity, {
            "nonce": "expired-nonce",
            "note_content": "Some note",
            "deal_id": "22222222-2222-2222-2222-222222222222",
            "organisation_id": "11111111-1111-1111-1111-111111111111",
        })

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_called_once_with("note_confirm", "expired-nonce")

    @patch("src.service.bots.form_handlers.add_deal_note_handler.validate_and_consume_nonce")
    async def test_rejects_missing_nonce(
        self,
        mock_validate: MagicMock,
        handler: AddDealNoteFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(activity, {
            "note_content": "Some note",
            "deal_id": "22222222-2222-2222-2222-222222222222",
        })

        assert result.success is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/bots/form_handlers/test_add_deal_note_handler_nonce.py -v`
Expected: FAIL

**Step 3: Add nonce validation**

Add import at top of `add_deal_note_handler.py`:

```python
from src.service.bots.form_handlers.nonce import validate_and_consume_nonce
```

Add at the start of `handle()`, before the `note_content` extraction:

```python
        # Validate one-time nonce
        nonce = form_data.get("nonce", "")
        if not nonce or not validate_and_consume_nonce("note_confirm", nonce):
            return FormHandlerResult(
                success=False,
                message=(
                    "This confirmation has expired or was already used. "
                    "Please ask the assistant to send a new one."
                ),
            )
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/service/bots/form_handlers/test_add_deal_note_handler_nonce.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add src/service/bots/form_handlers/add_deal_note_handler.py tests/service/bots/form_handlers/test_add_deal_note_handler_nonce.py
git commit -m "feat: add nonce validation to add deal note handler"
```

---

### Task 6: Add Nonce-Aware Cancel Handlers to Registry

**Files:**
- Modify: `src/service/bots/form_handlers/__init__.py`

**Context:** The existing cancel handlers are generic `CancelFormHandler("...cancelled.")`. These still work since they just return a success message. However, they should also consume the nonce so it can't be re-used. We'll create a small `NonceCancelFormHandler` that consumes the nonce before returning.

**Step 1: Write the failing test**

```python
# tests/service/bots/form_handlers/test_nonce_cancel_handler.py
"""Tests for nonce-aware cancel handlers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.service.bots.form_handlers.base import BaseFormHandler


class TestNonceCancelHandler:
    """Tests for NonceCancelFormHandler."""

    async def test_cancel_deal_consumes_nonce(self) -> None:
        from src.service.bots.form_handlers import FormHandlerRegistry

        handler = FormHandlerRegistry._handlers.get("create_deal_cancel")
        assert handler is not None

        with patch(
            "src.service.bots.form_handlers.nonce.get_redis_client"
        ) as mock_redis:
            mock_client = MagicMock()
            mock_client.delete.return_value = 1
            mock_redis.return_value = mock_client

            result = await handler.handle(
                {"from": {"id": "user-1"}, "channelData": {}},
                {"nonce": "test-nonce", "organisation_id": "org-1"},
            )

            assert result.success is True
            assert "cancelled" in result.message.lower()

    async def test_cancel_note_consumes_nonce(self) -> None:
        from src.service.bots.form_handlers import FormHandlerRegistry

        handler = FormHandlerRegistry._handlers.get("add_deal_note_cancel")
        assert handler is not None

        with patch(
            "src.service.bots.form_handlers.nonce.get_redis_client"
        ) as mock_redis:
            mock_client = MagicMock()
            mock_client.delete.return_value = 1
            mock_redis.return_value = mock_client

            result = await handler.handle(
                {"from": {"id": "user-1"}, "channelData": {}},
                {"nonce": "test-nonce", "organisation_id": "org-1"},
            )

            assert result.success is True
            assert "cancelled" in result.message.lower()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/bots/form_handlers/test_nonce_cancel_handler.py -v`
Expected: FAIL (current cancel handlers don't consume nonces)

**Step 3: Create NonceCancelFormHandler and update registry**

Add to `src/service/bots/form_handlers/base.py`:

```python
class NonceCancelFormHandler(BaseFormHandler):
    """Cancel handler that also consumes the Redis nonce."""

    def __init__(self, nonce_prefix: str, message: str = "Action cancelled."):
        self._nonce_prefix = nonce_prefix
        self._message = message

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        nonce = form_data.get("nonce", "")
        if nonce:
            from src.service.bots.form_handlers.nonce import validate_and_consume_nonce

            validate_and_consume_nonce(self._nonce_prefix, nonce)
        return FormHandlerResult(success=True, message=self._message)
```

Then in `__init__.py`, update the cancel handler registrations:

```python
from src.service.bots.form_handlers.base import (
    BaseFormHandler,
    CancelFormHandler,
    FormHandlerResult,
    NonceCancelFormHandler,
)

# Update these two registrations:
FormHandlerRegistry.register(
    "create_deal_cancel",
    NonceCancelFormHandler("deal_confirm", "Deal creation cancelled."),
)
FormHandlerRegistry.register(
    "add_deal_note_cancel",
    NonceCancelFormHandler("note_confirm", "Note cancelled."),
)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/service/bots/form_handlers/test_nonce_cancel_handler.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add src/service/bots/form_handlers/base.py src/service/bots/form_handlers/__init__.py tests/service/bots/form_handlers/test_nonce_cancel_handler.py
git commit -m "feat: add nonce-aware cancel handlers for deal and note"
```

---

### Task 7: Run Full Test Suite and Verify

**Files:** None (verification only)

**Step 1: Run all related tests**

Run: `uv run pytest tests/service/bots/form_handlers/ tests/agents/tools/crm/ -v`
Expected: All tests PASS (new and existing)

**Step 2: Run linter**

Run: `uv run ruff check src/agents/tools/crm/create_deal.py src/agents/tools/crm/add_deal_note.py src/service/bots/form_handlers/`
Expected: No errors

**Step 3: Run type checker**

Run: `uv run mypy src/agents/tools/crm/create_deal.py src/agents/tools/crm/add_deal_note.py src/service/bots/form_handlers/nonce.py src/service/bots/form_handlers/base.py`
Expected: No errors (or only pre-existing ones)

**Step 4: Fix any issues found, commit**

```bash
git add -u
git commit -m "fix: address lint and type issues"
```
