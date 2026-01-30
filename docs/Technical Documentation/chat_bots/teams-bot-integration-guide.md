# Microsoft Teams Bot Integration Guide

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Design](#2-architecture-design)
3. [Database Schema Changes](#3-database-schema-changes)
4. [Azure Bot Service Setup](#4-azure-bot-service-setup)
5. [Backend Implementation](#5-backend-implementation)
6. [Authentication Flow](#6-authentication-flow)
7. [Message Processing Pipeline](#7-message-processing-pipeline)
8. [Multi-Tenant Distribution](#8-multi-tenant-distribution)
9. [Testing & Debugging](#9-testing--debugging)
10. [Production Deployment](#10-production-deployment)

---

## 1. Overview

### 1.1 What We're Building

A Microsoft Teams bot that allows users from **different organizations** to:
1. Install the bot from Microsoft Teams App Store (AppSource)
2. Authenticate using **your custom login system** (not Microsoft)
3. Send messages and receive AI-powered answers based on their organization's documentation
4. Leverage your existing LLM + Knowledge Base infrastructure

### 1.2 Key Decisions

| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Bot Type | **Single-Tenant** | Multi-tenant deprecated after July 2025 |
| Distribution | **AppSource** | Allows cross-org installation |
| Auth | **Custom OAuth** | Your existing auth system |
| Database | **Reuse existing tables** | Add `channel` field to distinguish Teams |
| Processing | **Existing Celery workers** | Reuse chat_worker pipeline |

### 1.3 High-Level Flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Teams Client   │────▶│  Azure Bot       │────▶│  Your Backend       │
│  (User in Org)  │◀────│  Service         │◀────│  (FastAPI)          │
└─────────────────┘     └──────────────────┘     └─────────────────────┘
                                                          │
                                                          ▼
                                                 ┌─────────────────────┐
                                                 │  Celery Worker      │
                                                 │  (chat_worker)      │
                                                 └─────────────────────┘
                                                          │
                                                          ▼
                                                 ┌─────────────────────┐
                                                 │  LLM + Knowledge    │
                                                 │  Base               │
                                                 └─────────────────────┘
```

---

## 2. Architecture Design

### 2.1 Component Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         MICROSOFT TEAMS                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                      │
│  │ Org A User  │  │ Org B User  │  │ Org C User  │                      │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                      │
└─────────┼────────────────┼────────────────┼─────────────────────────────┘
          │                │                │
          ▼                ▼                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    AZURE BOT SERVICE                                     │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  Bot Connector (handles auth, message routing, channel adapters) │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ HTTPS POST /api/teams/messages
┌─────────────────────────────────────────────────────────────────────────┐
│                      YOUR BACKEND (FastAPI)                              │
│                                                                          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐          │
│  │ Teams Webhook   │  │ Auth Service    │  │ Chat Service    │          │
│  │ Handler         │──│ (Custom Login)  │──│ (Existing)      │          │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘          │
│           │                                         │                    │
│           ▼                                         ▼                    │
│  ┌─────────────────┐                      ┌─────────────────┐           │
│  │ Teams User      │                      │ ChatMessageJob  │           │
│  │ Mapping Table   │                      │ (Celery Queue)  │           │
│  └─────────────────┘                      └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────┘
                                                      │
                                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        CELERY CHAT WORKER                                │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐          │
│  │ Message         │  │ GeneralAgent    │  │ Teams Response  │          │
│  │ Processing      │──│ (LLM + Tools)   │──│ Sender          │          │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Integration Points with Existing System

| Existing Component | Teams Integration |
|--------------------|-------------------|
| `chat_sessions` | Reuse with `channel = 'teams'` |
| `chat_messages` | Reuse with Teams metadata |
| `chat_message_jobs` | Reuse existing job system |
| `GeneralAgent` | Reuse for message processing |
| `chat_worker` | Add Teams response task |
| `users` table | Link via `teams_user_mappings` |

---

## 3. Database Schema Changes

### 3.1 Overview: Minimal Changes Required

Your existing schema is well-designed. We need:
1. **One new table**: `teams_user_mappings` (in org schema)
2. **One field addition**: `channel` enum on `chat_sessions`
3. **Metadata storage**: Use existing `extra` JSONB fields

### 3.2 New Table: Teams User Mappings

Create migration file: `migrations/org_migrations/versions/xxx_add_teams_user_mappings.py`

```python
"""Add Teams user mappings table

Revision ID: xxx
Revises: previous_revision
Create Date: 2024-xx-xx
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = 'xxx'
down_revision = 'previous_revision'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'teams_user_mappings',
        sa.Column('mapping_id', UUID(as_uuid=True), primary_key=True),
        sa.Column('teams_user_id', sa.String(255), nullable=False, index=True),
        sa.Column('teams_tenant_id', sa.String(255), nullable=False, index=True),
        sa.Column('teams_conversation_id', sa.String(500), nullable=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('public.users.user_id'), nullable=True),
        sa.Column('organisation_id', UUID(as_uuid=True), nullable=False),
        sa.Column('is_authenticated', sa.Boolean(), default=False, nullable=False),
        sa.Column('auth_token', sa.String(500), nullable=True),  # Your app's session token
        sa.Column('auth_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('teams_user_name', sa.String(255), nullable=True),
        sa.Column('teams_user_email', sa.String(255), nullable=True),
        sa.Column('extra', JSONB, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.UniqueConstraint('teams_user_id', 'teams_tenant_id', name='uq_teams_user_tenant'),
    )

    # Index for fast lookups
    op.create_index(
        'ix_teams_user_mappings_lookup',
        'teams_user_mappings',
        ['teams_user_id', 'teams_tenant_id', 'organisation_id']
    )


def downgrade() -> None:
    op.drop_index('ix_teams_user_mappings_lookup')
    op.drop_table('teams_user_mappings')
```

### 3.3 Model Definition

Create file: `src/domain/models/organisation_tables/teams_models.py`

```python
"""Teams integration models."""

from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional

from sqlmodel import SQLModel, Field, Column
from sqlalchemy.dialects.postgresql import JSONB


class TeamsUserMapping(SQLModel, table=True):
    """
    Maps Microsoft Teams users to your application users.

    This table lives in each org schema (org_*) to maintain tenant isolation.
    A Teams user must authenticate with your system before they can use the bot.
    """

    __tablename__ = "teams_user_mappings"

    # Primary key
    mapping_id: UUID = Field(default_factory=uuid4, primary_key=True)

    # Teams identifiers (from Bot Framework activity)
    teams_user_id: str = Field(max_length=255, index=True)
    teams_tenant_id: str = Field(max_length=255, index=True)
    teams_conversation_id: Optional[str] = Field(default=None, max_length=500)

    # Your application user (linked after authentication)
    user_id: Optional[UUID] = Field(default=None, foreign_key="public.users.user_id")
    organisation_id: UUID = Field(nullable=False)

    # Authentication state
    is_authenticated: bool = Field(default=False)
    auth_token: Optional[str] = Field(default=None, max_length=500)
    auth_expires_at: Optional[datetime] = Field(default=None)

    # Cached Teams user info
    teams_user_name: Optional[str] = Field(default=None, max_length=255)
    teams_user_email: Optional[str] = Field(default=None, max_length=255)

    # Flexible metadata
    extra: Optional[dict] = Field(default=None, sa_column=Column(JSONB))

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = Field(default=None)


class TeamsConversationReference(SQLModel):
    """
    Stores conversation reference for proactive messaging.
    Store this in TeamsUserMapping.extra or a separate table if needed.
    """

    activity_id: Optional[str] = None
    user_id: str
    bot_id: str
    conversation_id: str
    channel_id: str
    service_url: str
    tenant_id: str
```

### 3.4 Update ChatSession Model

Add channel support to existing `ChatSession` model.

Edit: `src/domain/models/organisation_tables/chat_models.py`

```python
from enum import Enum

class ChatChannel(str, Enum):
    """Channel through which chat session was created."""
    WEB = "web"           # Your web application
    TEAMS = "teams"       # Microsoft Teams
    SLACK = "slack"       # Future: Slack
    API = "api"           # Direct API access


class ChatSession(SQLModel, table=True):
    # ... existing fields ...

    # ADD THIS FIELD
    channel: ChatChannel = Field(default=ChatChannel.WEB)

    # ADD: Store channel-specific metadata
    # For Teams: { "teams_conversation_id": "...", "teams_tenant_id": "..." }
    channel_metadata: Optional[dict] = Field(default=None, sa_column=Column(JSONB))
```

Migration for this change:

```python
"""Add channel field to chat_sessions

Revision ID: yyy
"""

from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    # Add channel enum type
    op.execute("CREATE TYPE chatchannel AS ENUM ('web', 'teams', 'slack', 'api')")

    # Add channel column with default 'web'
    op.add_column(
        'chat_sessions',
        sa.Column('channel', sa.Enum('web', 'teams', 'slack', 'api', name='chatchannel'),
                  server_default='web', nullable=False)
    )

    # Add channel_metadata JSONB column
    op.add_column(
        'chat_sessions',
        sa.Column('channel_metadata', sa.dialects.postgresql.JSONB(), nullable=True)
    )

def downgrade() -> None:
    op.drop_column('chat_sessions', 'channel_metadata')
    op.drop_column('chat_sessions', 'channel')
    op.execute("DROP TYPE chatchannel")
```

### 3.5 Summary: Do You Need a Separate Table?

**Answer: Mostly NO - Reuse existing tables with minimal additions**

| Table | Action | Notes |
|-------|--------|-------|
| `chat_sessions` | **Reuse** | Add `channel` field |
| `chat_messages` | **Reuse** | Store Teams metadata in `extra` |
| `chat_message_jobs` | **Reuse** | Add job_type for Teams response |
| `users` | **Reuse** | Teams users link to your users |
| `teams_user_mappings` | **NEW** | Maps Teams→Your users |

---

## 4. Azure Bot Service Setup

### 4.1 Prerequisites

1. Azure subscription
2. Microsoft 365 Developer tenant (or regular tenant with admin access)
3. Your backend deployed and accessible via HTTPS

### 4.2 Step 1: Create Azure Bot Resource

1. Go to [Azure Portal](https://portal.azure.com)
2. Click **Create a resource** → Search "Azure Bot"
3. Click **Create**

**Configuration:**

| Field | Value |
|-------|-------|
| Bot handle | `copilot-gtm-bot` (unique name) |
| Subscription | Your subscription |
| Resource group | Create new or use existing |
| Pricing tier | **F0 (Free)** for dev, **S1** for production |
| Type of App | **Single Tenant** (IMPORTANT!) |
| Creation type | **Create new Microsoft App ID** |
| Microsoft App ID | Let Azure create |

4. Click **Review + Create** → **Create**

### 4.3 Step 2: Configure Bot Messaging Endpoint

1. After creation, go to your Bot resource
2. Click **Configuration** in left menu
3. Set **Messaging endpoint**:
   ```
   https://your-domain.com/api/teams/messages
   ```
4. Click **Save**

### 4.4 Step 3: Get App Credentials

1. In Bot resource → **Configuration**
2. Copy **Microsoft App ID** (this is your `BOT_APP_ID`)
3. Click **Manage Password** → **New client secret**
4. Copy the secret value (this is your `BOT_APP_SECRET`)

**Store these in your environment:**

```env
# .env
TEAMS_BOT_APP_ID=your-app-id-here
TEAMS_BOT_APP_SECRET=your-secret-here
TEAMS_BOT_TENANT_ID=your-azure-tenant-id  # For single-tenant
```

### 4.5 Step 4: Enable Teams Channel

1. In Bot resource → **Channels**
2. Click **Microsoft Teams** icon
3. Click **Apply** to accept terms
4. Teams channel is now enabled

### 4.6 Step 5: Create Teams App Manifest

Create a Teams app package (ZIP file containing):

**manifest.json:**

```json
{
  "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
  "manifestVersion": "1.16",
  "version": "1.0.0",
  "id": "YOUR-BOT-APP-ID-HERE",
  "packageName": "com.yourcompany.copilotgtm",
  "developer": {
    "name": "Your Company Name",
    "websiteUrl": "https://yourcompany.com",
    "privacyUrl": "https://yourcompany.com/privacy",
    "termsOfUseUrl": "https://yourcompany.com/terms"
  },
  "name": {
    "short": "CopilotGTM",
    "full": "CopilotGTM AI Assistant"
  },
  "description": {
    "short": "AI-powered sales assistant",
    "full": "Get AI-powered answers based on your organization's documentation and deal information."
  },
  "icons": {
    "color": "color.png",
    "outline": "outline.png"
  },
  "accentColor": "#5558AF",
  "bots": [
    {
      "botId": "YOUR-BOT-APP-ID-HERE",
      "scopes": ["personal", "team", "groupchat"],
      "supportsFiles": false,
      "isNotificationOnly": false,
      "commandLists": [
        {
          "scopes": ["personal"],
          "commands": [
            {
              "title": "login",
              "description": "Sign in to your CopilotGTM account"
            },
            {
              "title": "logout",
              "description": "Sign out of your account"
            },
            {
              "title": "status",
              "description": "Check your authentication status"
            },
            {
              "title": "help",
              "description": "Get help using the bot"
            }
          ]
        }
      ]
    }
  ],
  "permissions": ["identity", "messageTeamMembers"],
  "validDomains": [
    "your-domain.com",
    "token.botframework.com"
  ],
  "webApplicationInfo": {
    "id": "YOUR-BOT-APP-ID-HERE",
    "resource": "api://your-domain.com/YOUR-BOT-APP-ID-HERE"
  }
}
```

**Icons required:**
- `color.png`: 192x192 pixels, full color
- `outline.png`: 32x32 pixels, transparent outline

**Create the package:**
```bash
zip -r copilotgtm-teams-app.zip manifest.json color.png outline.png
```

---

## 5. Backend Implementation

### 5.1 Project Structure

```
src/
├── api/
│   └── routers/
│       └── teams/
│           ├── __init__.py
│           ├── webhook_handler.py      # Bot message endpoint
│           ├── auth_handler.py         # Custom auth endpoints
│           └── schemas.py              # Teams-specific schemas
├── service/
│   └── teams/
│       ├── __init__.py
│       ├── bot_service.py              # Bot Framework integration
│       ├── teams_auth_service.py       # Custom auth for Teams
│       └── teams_message_service.py    # Message handling
├── domain/
│   └── models/
│       └── organisation_tables/
│           └── teams_models.py         # TeamsUserMapping model
└── workers/
    └── chat_worker/
        └── tasks/
            └── teams_response.py       # Send response to Teams
```

### 5.2 Install Dependencies

Add to `pyproject.toml`:

```toml
[project.dependencies]
# ... existing deps ...
botbuilder-core = "^4.15.0"
botbuilder-schema = "^4.15.0"
botbuilder-integration-aiohttp = "^4.15.0"
```

Or install via uv:
```bash
uv add botbuilder-core botbuilder-schema botbuilder-integration-aiohttp
```

### 5.3 Configuration

Create: `src/infra/config/teams_config.py`

```python
"""Teams Bot configuration."""

from pydantic_settings import BaseSettings
from functools import lru_cache


class TeamsConfig(BaseSettings):
    """Configuration for Microsoft Teams bot integration."""

    # Azure Bot Service credentials
    bot_app_id: str = ""
    bot_app_secret: str = ""
    bot_tenant_id: str = ""  # Your Azure tenant ID for single-tenant

    # Your custom auth
    auth_login_url: str = ""  # URL to your login page
    auth_callback_url: str = ""  # Callback after login

    # Feature flags
    teams_enabled: bool = False

    class Config:
        env_prefix = "TEAMS_"
        env_file = ".env"


@lru_cache()
def get_teams_config() -> TeamsConfig:
    return TeamsConfig()
```

### 5.4 Bot Service Implementation

Create: `src/service/teams/bot_service.py`

```python
"""Microsoft Teams Bot Service using Bot Framework."""

import logging
from typing import Optional
from uuid import UUID

from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
    ConversationState,
    UserState,
    MemoryStorage,
)
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ConversationReference,
    Attachment,
    HeroCard,
    CardAction,
    ActionTypes,
)

from src.infra.config.teams_config import get_teams_config

logger = logging.getLogger(__name__)


class TeamsBotService:
    """
    Handles Microsoft Teams bot interactions.

    This service:
    1. Receives messages from Teams via Bot Framework
    2. Validates authentication state
    3. Routes messages to your existing chat system
    4. Sends responses back to Teams
    """

    def __init__(self):
        config = get_teams_config()

        # Bot Framework adapter settings
        settings = BotFrameworkAdapterSettings(
            app_id=config.bot_app_id,
            app_password=config.bot_app_secret,
        )

        self.adapter = BotFrameworkAdapter(settings)

        # Storage for conversation state (use Redis in production)
        storage = MemoryStorage()
        self.conversation_state = ConversationState(storage)
        self.user_state = UserState(storage)

        # Error handler
        self.adapter.on_turn_error = self._handle_error

    async def _handle_error(self, context: TurnContext, error: Exception):
        """Handle errors during bot turn."""
        logger.error(f"Teams bot error: {error}", exc_info=True)
        await context.send_activity("Sorry, something went wrong. Please try again.")

    async def process_activity(self, activity: Activity, auth_header: str) -> Optional[Activity]:
        """
        Process incoming activity from Teams.

        This is called by the webhook handler.
        """
        response = None

        async def turn_callback(turn_context: TurnContext):
            nonlocal response
            response = await self._handle_turn(turn_context)

        await self.adapter.process_activity(activity, auth_header, turn_callback)
        return response

    async def _handle_turn(self, context: TurnContext) -> Optional[Activity]:
        """Handle a single conversation turn."""
        activity = context.activity

        if activity.type == ActivityTypes.message:
            return await self._handle_message(context)

        elif activity.type == ActivityTypes.conversation_update:
            return await self._handle_conversation_update(context)

        elif activity.type == ActivityTypes.invoke:
            return await self._handle_invoke(context)

        return None

    async def _handle_message(self, context: TurnContext) -> None:
        """Handle incoming message from user."""
        activity = context.activity
        text = activity.text.strip() if activity.text else ""

        # Extract Teams user info
        teams_user_id = activity.from_property.id
        teams_tenant_id = activity.channel_data.get("tenant", {}).get("id", "")
        teams_user_name = activity.from_property.name

        logger.info(f"Teams message from {teams_user_name}: {text[:50]}...")

        # Handle commands
        if text.lower() == "login":
            await self._send_login_card(context)
            return

        if text.lower() == "logout":
            await self._handle_logout(context)
            return

        if text.lower() == "status":
            await self._send_status(context)
            return

        if text.lower() == "help":
            await self._send_help(context)
            return

        # Check if user is authenticated
        # This will be implemented in teams_auth_service.py
        from src.service.teams.teams_auth_service import TeamsAuthService

        auth_service = TeamsAuthService()
        user_mapping = await auth_service.get_user_mapping(
            teams_user_id=teams_user_id,
            teams_tenant_id=teams_tenant_id,
        )

        if not user_mapping or not user_mapping.is_authenticated:
            await self._send_login_required(context)
            return

        # User is authenticated - process the message
        await self._process_authenticated_message(
            context=context,
            user_mapping=user_mapping,
            message_text=text,
        )

    async def _send_login_card(self, context: TurnContext) -> None:
        """Send a card with login button."""
        config = get_teams_config()

        # Generate state token for OAuth flow
        activity = context.activity
        state_token = self._generate_state_token(
            teams_user_id=activity.from_property.id,
            teams_tenant_id=activity.channel_data.get("tenant", {}).get("id", ""),
            conversation_id=activity.conversation.id,
        )

        login_url = f"{config.auth_login_url}?state={state_token}&channel=teams"

        card = HeroCard(
            title="Sign In Required",
            text="Please sign in to your CopilotGTM account to use this bot.",
            buttons=[
                CardAction(
                    type=ActionTypes.open_url,
                    title="Sign In",
                    value=login_url,
                )
            ],
        )

        attachment = Attachment(
            content_type="application/vnd.microsoft.card.hero",
            content=card,
        )

        reply = Activity(
            type=ActivityTypes.message,
            attachments=[attachment],
        )

        await context.send_activity(reply)

    async def _send_login_required(self, context: TurnContext) -> None:
        """Prompt user to login when they try to send a message."""
        card = HeroCard(
            title="Authentication Required",
            text="You need to sign in before sending messages. Type 'login' or click the button below.",
            buttons=[
                CardAction(
                    type=ActionTypes.im_back,
                    title="Sign In",
                    value="login",
                )
            ],
        )

        attachment = Attachment(
            content_type="application/vnd.microsoft.card.hero",
            content=card,
        )

        await context.send_activity(Activity(
            type=ActivityTypes.message,
            attachments=[attachment],
        ))

    async def _send_help(self, context: TurnContext) -> None:
        """Send help message."""
        help_text = """
**CopilotGTM Bot Commands:**

• **login** - Sign in to your account
• **logout** - Sign out
• **status** - Check authentication status
• **help** - Show this help message

Once signed in, just type your question and I'll help you find answers from your organization's knowledge base!
        """
        await context.send_activity(help_text)

    async def _send_status(self, context: TurnContext) -> None:
        """Send authentication status."""
        activity = context.activity
        teams_user_id = activity.from_property.id
        teams_tenant_id = activity.channel_data.get("tenant", {}).get("id", "")

        from src.service.teams.teams_auth_service import TeamsAuthService

        auth_service = TeamsAuthService()
        user_mapping = await auth_service.get_user_mapping(
            teams_user_id=teams_user_id,
            teams_tenant_id=teams_tenant_id,
        )

        if user_mapping and user_mapping.is_authenticated:
            await context.send_activity(
                f"✅ You are signed in as **{user_mapping.teams_user_email or 'User'}**\n\n"
                f"Organization: Your organization\n"
                f"Type 'logout' to sign out."
            )
        else:
            await context.send_activity(
                "❌ You are not signed in.\n\nType 'login' to sign in."
            )

    async def _handle_logout(self, context: TurnContext) -> None:
        """Handle logout command."""
        activity = context.activity
        teams_user_id = activity.from_property.id
        teams_tenant_id = activity.channel_data.get("tenant", {}).get("id", "")

        from src.service.teams.teams_auth_service import TeamsAuthService

        auth_service = TeamsAuthService()
        await auth_service.logout_user(
            teams_user_id=teams_user_id,
            teams_tenant_id=teams_tenant_id,
        )

        await context.send_activity("✅ You have been signed out. Type 'login' to sign in again.")

    async def _process_authenticated_message(
        self,
        context: TurnContext,
        user_mapping,  # TeamsUserMapping
        message_text: str,
    ) -> None:
        """
        Process message from authenticated user.

        This integrates with your existing chat system.
        """
        activity = context.activity

        # Send typing indicator
        await context.send_activity(Activity(type=ActivityTypes.typing))

        # Store conversation reference for async response
        conversation_ref = TurnContext.get_conversation_reference(activity)

        # Import your existing services
        from src.service.teams.teams_message_service import TeamsMessageService

        message_service = TeamsMessageService()

        # This will:
        # 1. Create/get chat session
        # 2. Create user message
        # 3. Create job and enqueue to Celery
        # 4. The worker will process and send response back to Teams
        await message_service.process_teams_message(
            user_mapping=user_mapping,
            message_text=message_text,
            conversation_reference=conversation_ref,
        )

        # Note: Response is sent asynchronously by the Celery worker
        # The user will see the typing indicator until response arrives

    async def _handle_conversation_update(self, context: TurnContext) -> None:
        """Handle conversation update (user added/removed, bot installed)."""
        activity = context.activity

        if activity.members_added:
            for member in activity.members_added:
                if member.id != activity.recipient.id:
                    # New user added - send welcome message
                    await self._send_welcome(context)

    async def _send_welcome(self, context: TurnContext) -> None:
        """Send welcome message to new users."""
        welcome_text = """
👋 **Welcome to CopilotGTM!**

I'm your AI-powered sales assistant. I can help you find answers from your organization's documentation and deal information.

**To get started:**
1. Type **login** to sign in to your account
2. Once signed in, just ask me any question!

Type **help** to see all available commands.
        """
        await context.send_activity(welcome_text)

    async def _handle_invoke(self, context: TurnContext) -> None:
        """Handle invoke activities (adaptive card actions, etc.)."""
        # Handle adaptive card submit actions if needed
        pass

    def _generate_state_token(
        self,
        teams_user_id: str,
        teams_tenant_id: str,
        conversation_id: str,
    ) -> str:
        """Generate state token for OAuth flow."""
        import base64
        import json

        state = {
            "teams_user_id": teams_user_id,
            "teams_tenant_id": teams_tenant_id,
            "conversation_id": conversation_id,
        }

        return base64.urlsafe_b64encode(json.dumps(state).encode()).decode()

    async def send_proactive_message(
        self,
        conversation_reference: dict,
        message: str,
    ) -> None:
        """
        Send a proactive message to Teams.

        Used by Celery worker to send responses.
        """
        conversation_ref = ConversationReference(**conversation_reference)

        async def send_callback(turn_context: TurnContext):
            await turn_context.send_activity(message)

        await self.adapter.continue_conversation(
            conversation_ref,
            send_callback,
            get_teams_config().bot_app_id,
        )
```

### 5.5 Webhook Handler

Create: `src/api/routers/teams/webhook_handler.py`

```python
"""Teams Bot webhook handler."""

import logging
from typing import Annotated

from fastapi import APIRouter, Request, Response, Depends, HTTPException, status
from botbuilder.schema import Activity

from src.service.teams.bot_service import TeamsBotService
from src.infra.config.teams_config import get_teams_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/teams", tags=["teams"])


def get_bot_service() -> TeamsBotService:
    """Dependency to get bot service instance."""
    return TeamsBotService()


@router.post("/messages")
async def handle_teams_message(
    request: Request,
    bot_service: Annotated[TeamsBotService, Depends(get_bot_service)],
):
    """
    Webhook endpoint for Microsoft Teams messages.

    This endpoint receives all messages from Teams via Azure Bot Service.
    """
    config = get_teams_config()

    if not config.teams_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Teams integration is not enabled",
        )

    # Get authorization header for Bot Framework validation
    auth_header = request.headers.get("Authorization", "")

    # Parse the incoming activity
    body = await request.json()
    activity = Activity().deserialize(body)

    logger.info(f"Received Teams activity: type={activity.type}, from={activity.from_property.id if activity.from_property else 'unknown'}")

    try:
        # Process the activity
        await bot_service.process_activity(activity, auth_header)
        return Response(status_code=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Error processing Teams activity: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process message",
        )


@router.get("/health")
async def teams_health():
    """Health check endpoint for Teams bot."""
    config = get_teams_config()
    return {
        "status": "healthy",
        "teams_enabled": config.teams_enabled,
        "bot_configured": bool(config.bot_app_id),
    }
```

### 5.6 Auth Service for Teams

Create: `src/service/teams/teams_auth_service.py`

```python
"""Authentication service for Teams users."""

import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.teams_models import TeamsUserMapping
from src.domain.models.master_tables.user_models import User
from src.infra.database.organisation_session import acquire_org_async_session
from src.infra.database.master_session import acquire_master_async_session

logger = logging.getLogger(__name__)


class TeamsAuthService:
    """
    Handles authentication for Teams users.

    Flow:
    1. User sends message in Teams
    2. Bot checks if user is authenticated
    3. If not, sends login card with link to YOUR login page
    4. User logs in on your site
    5. Your site calls callback to link Teams user to your user
    6. Future messages are authenticated
    """

    async def get_user_mapping(
        self,
        teams_user_id: str,
        teams_tenant_id: str,
    ) -> Optional[TeamsUserMapping]:
        """
        Get existing user mapping for Teams user.

        This searches across all orgs to find the mapping.
        In production, you might want to cache this.
        """
        # First, try to find which org this user belongs to
        # This is a simplified approach - you might want to maintain
        # a master-level lookup table for performance

        from src.infra.database.master_session import get_all_org_ids

        org_ids = await get_all_org_ids()

        for org_id in org_ids:
            async for session in acquire_org_async_session(org_id):
                stmt = select(TeamsUserMapping).where(
                    TeamsUserMapping.teams_user_id == teams_user_id,
                    TeamsUserMapping.teams_tenant_id == teams_tenant_id,
                )
                result = await session.execute(stmt)
                mapping = result.scalar_one_or_none()

                if mapping:
                    # Check if auth is still valid
                    if mapping.auth_expires_at and mapping.auth_expires_at < datetime.utcnow():
                        mapping.is_authenticated = False
                        await session.commit()

                    return mapping

        return None

    async def create_or_update_mapping(
        self,
        organisation_id: UUID,
        user_id: UUID,
        teams_user_id: str,
        teams_tenant_id: str,
        teams_conversation_id: str,
        teams_user_name: Optional[str] = None,
        teams_user_email: Optional[str] = None,
        auth_token: Optional[str] = None,
    ) -> TeamsUserMapping:
        """
        Create or update Teams user mapping after successful authentication.

        Called by your auth callback when user completes login.
        """
        async for session in acquire_org_async_session(organisation_id):
            # Check if mapping exists
            stmt = select(TeamsUserMapping).where(
                TeamsUserMapping.teams_user_id == teams_user_id,
                TeamsUserMapping.teams_tenant_id == teams_tenant_id,
            )
            result = await session.execute(stmt)
            mapping = result.scalar_one_or_none()

            if mapping:
                # Update existing mapping
                mapping.user_id = user_id
                mapping.organisation_id = organisation_id
                mapping.is_authenticated = True
                mapping.auth_token = auth_token
                mapping.auth_expires_at = datetime.utcnow() + timedelta(days=30)
                mapping.teams_user_name = teams_user_name
                mapping.teams_user_email = teams_user_email
                mapping.teams_conversation_id = teams_conversation_id
                mapping.updated_at = datetime.utcnow()
            else:
                # Create new mapping
                mapping = TeamsUserMapping(
                    mapping_id=uuid4(),
                    teams_user_id=teams_user_id,
                    teams_tenant_id=teams_tenant_id,
                    teams_conversation_id=teams_conversation_id,
                    user_id=user_id,
                    organisation_id=organisation_id,
                    is_authenticated=True,
                    auth_token=auth_token,
                    auth_expires_at=datetime.utcnow() + timedelta(days=30),
                    teams_user_name=teams_user_name,
                    teams_user_email=teams_user_email,
                )
                session.add(mapping)

            await session.commit()
            await session.refresh(mapping)

            return mapping

    async def logout_user(
        self,
        teams_user_id: str,
        teams_tenant_id: str,
    ) -> bool:
        """Logout Teams user by clearing auth state."""
        mapping = await self.get_user_mapping(teams_user_id, teams_tenant_id)

        if not mapping:
            return False

        async for session in acquire_org_async_session(mapping.organisation_id):
            stmt = select(TeamsUserMapping).where(
                TeamsUserMapping.mapping_id == mapping.mapping_id,
            )
            result = await session.execute(stmt)
            db_mapping = result.scalar_one_or_none()

            if db_mapping:
                db_mapping.is_authenticated = False
                db_mapping.auth_token = None
                db_mapping.auth_expires_at = None
                db_mapping.updated_at = datetime.utcnow()
                await session.commit()
                return True

        return False

    async def get_user_by_mapping(
        self,
        mapping: TeamsUserMapping,
    ) -> Optional[User]:
        """Get the application User for a Teams mapping."""
        if not mapping.user_id:
            return None

        async for session in acquire_master_async_session():
            stmt = select(User).where(User.user_id == mapping.user_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
```

### 5.7 Auth Callback Handler

Create: `src/api/routers/teams/auth_handler.py`

```python
"""Teams authentication callback handler."""

import logging
import base64
import json
from typing import Annotated

from fastapi import APIRouter, Request, Depends, HTTPException, status, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.teams.teams_auth_service import TeamsAuthService
from src.service.teams.bot_service import TeamsBotService
from src.infra.auth.user_auth import get_current_user
from src.domain.models.master_tables.user_models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/teams/auth", tags=["teams-auth"])


class TeamsAuthCallbackRequest(BaseModel):
    """Request body for Teams auth callback."""
    state: str  # Base64 encoded state from login URL


@router.get("/callback")
async def teams_auth_callback(
    state: Annotated[str, Query(...)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Callback endpoint after user authenticates.

    Flow:
    1. User clicks login link in Teams
    2. User is redirected to your login page with state token
    3. User logs in with your auth system
    4. Your login page redirects here with the state token
    5. We link the Teams user to your application user
    6. We send a confirmation message to Teams
    """
    try:
        # Decode state token
        state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())

        teams_user_id = state_data.get("teams_user_id")
        teams_tenant_id = state_data.get("teams_tenant_id")
        conversation_id = state_data.get("conversation_id")

        if not all([teams_user_id, teams_tenant_id]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid state token",
            )

        # Link Teams user to application user
        auth_service = TeamsAuthService()
        mapping = await auth_service.create_or_update_mapping(
            organisation_id=current_user.organisation_id,
            user_id=current_user.user_id,
            teams_user_id=teams_user_id,
            teams_tenant_id=teams_tenant_id,
            teams_conversation_id=conversation_id,
            teams_user_email=current_user.email,
            teams_user_name=f"{current_user.first_name} {current_user.last_name}".strip(),
        )

        logger.info(
            f"Teams user {teams_user_id} linked to user {current_user.user_id} "
            f"in org {current_user.organisation_id}"
        )

        # Send confirmation to Teams
        if conversation_id and mapping.extra and "conversation_reference" in mapping.extra:
            bot_service = TeamsBotService()
            await bot_service.send_proactive_message(
                conversation_reference=mapping.extra["conversation_reference"],
                message="✅ You are now signed in! You can start asking questions.",
            )

        # Return success page
        return HTMLResponse(content="""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Authentication Successful</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                }
                .card {
                    background: white;
                    padding: 40px;
                    border-radius: 12px;
                    box-shadow: 0 4px 20px rgba(0,0,0,0.15);
                    text-align: center;
                    max-width: 400px;
                }
                h1 { color: #22c55e; margin-bottom: 16px; }
                p { color: #666; line-height: 1.6; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>✅ Success!</h1>
                <p>You are now signed in to CopilotGTM.</p>
                <p>You can close this window and return to Microsoft Teams.</p>
            </div>
        </body>
        </html>
        """)

    except Exception as e:
        logger.error(f"Teams auth callback error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication failed",
        )
```

### 5.8 Message Service (Integration with Existing Chat)

Create: `src/service/teams/teams_message_service.py`

```python
"""Teams message service - integrates with existing chat system."""

import logging
from typing import Optional
from uuid import uuid4

from botbuilder.schema import ConversationReference

from src.domain.models.organisation_tables.teams_models import TeamsUserMapping
from src.domain.models.organisation_tables.chat_models import (
    ChatSession,
    ChatMessage,
    ChatMessageJob,
    ChatChannel,
    ChatType,
    MessageRole,
    MessageType,
    JobStatus,
)
from src.infra.database.organisation_session import acquire_org_async_session
from src.workers.chat_worker.client import ChatWorkerClient

logger = logging.getLogger(__name__)


class TeamsMessageService:
    """
    Handles Teams messages by integrating with existing chat system.

    This service:
    1. Creates/retrieves chat session for Teams conversation
    2. Stores user message using existing ChatMessage model
    3. Creates ChatMessageJob and enqueues to existing chat_worker
    4. The worker processes and sends response back to Teams
    """

    async def process_teams_message(
        self,
        user_mapping: TeamsUserMapping,
        message_text: str,
        conversation_reference: ConversationReference,
    ) -> str:
        """
        Process incoming message from Teams.

        Returns the job_id for tracking.
        """
        org_id = user_mapping.organisation_id
        user_id = user_mapping.user_id
        teams_conversation_id = conversation_reference.conversation.id

        async for session in acquire_org_async_session(org_id):
            # Get or create chat session for this Teams conversation
            chat_session = await self._get_or_create_session(
                session=session,
                org_id=org_id,
                user_id=user_id,
                teams_conversation_id=teams_conversation_id,
                conversation_reference=conversation_reference,
            )

            # Create user message
            user_message = ChatMessage(
                message_id=uuid4(),
                session_id=chat_session.session_id,
                role=MessageRole.USER,
                message_type=MessageType.GENERAL_MESSAGE,
                content={"text": message_text},
                extra={
                    "channel": "teams",
                    "teams_activity_id": conversation_reference.activity_id,
                },
            )
            session.add(user_message)

            # Create placeholder assistant message
            assistant_message = ChatMessage(
                message_id=uuid4(),
                session_id=chat_session.session_id,
                role=MessageRole.ASSISTANT,
                message_type=MessageType.GENERAL_MESSAGE,
                content={"text": ""},  # Will be filled by worker
                extra={
                    "channel": "teams",
                    "pending": True,
                },
            )
            session.add(assistant_message)

            # Create job
            job = ChatMessageJob(
                job_id=uuid4(),
                message_id=assistant_message.message_id,
                job_type="teams_chat_response",
                status=JobStatus.PENDING,
                job_metadata={
                    "session_id": str(chat_session.session_id),
                    "user_message_id": str(user_message.message_id),
                    "assistant_message_id": str(assistant_message.message_id),
                    "channel": "teams",
                    "conversation_reference": self._serialize_conversation_ref(conversation_reference),
                    "organisation_id": str(org_id),
                    "user_id": str(user_id),
                },
            )
            session.add(job)

            await session.commit()

            # Enqueue to Celery worker
            chat_client = ChatWorkerClient()
            chat_client.enqueue_teams_message_processing(
                job_id=str(job.job_id),
                organisation_id=str(org_id),
            )

            logger.info(f"Teams message job created: {job.job_id}")

            return str(job.job_id)

    async def _get_or_create_session(
        self,
        session,
        org_id,
        user_id,
        teams_conversation_id: str,
        conversation_reference: ConversationReference,
    ) -> ChatSession:
        """Get existing session for Teams conversation or create new one."""
        from sqlmodel import select

        # Look for existing session
        stmt = select(ChatSession).where(
            ChatSession.organisation_id == org_id,
            ChatSession.channel == ChatChannel.TEAMS,
            ChatSession.channel_metadata["teams_conversation_id"].astext == teams_conversation_id,
            ChatSession.is_deleted == False,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            return existing

        # Create new session
        new_session = ChatSession(
            session_id=uuid4(),
            user_id=user_id,
            organisation_id=org_id,
            title="Teams Conversation",
            chat_type=ChatType.GENERAL_CHAT,
            channel=ChatChannel.TEAMS,
            channel_metadata={
                "teams_conversation_id": teams_conversation_id,
                "teams_tenant_id": conversation_reference.channel_id,
                "service_url": conversation_reference.service_url,
            },
        )
        session.add(new_session)
        await session.flush()

        return new_session

    def _serialize_conversation_ref(self, ref: ConversationReference) -> dict:
        """Serialize conversation reference for storage."""
        return {
            "activity_id": ref.activity_id,
            "user_id": ref.user.id if ref.user else None,
            "bot_id": ref.bot.id if ref.bot else None,
            "conversation_id": ref.conversation.id if ref.conversation else None,
            "conversation_name": ref.conversation.name if ref.conversation else None,
            "channel_id": ref.channel_id,
            "service_url": ref.service_url,
        }
```

### 5.9 Celery Task for Teams Response

Create: `src/workers/chat_worker/tasks/teams_response.py`

```python
"""Celery task for processing Teams messages and sending responses."""

import logging
from uuid import UUID

from src.workers.chat_worker.celery_app import celery_app
from src.workers.chat_worker.services.teams_processing_service import TeamsProcessingService

logger = logging.getLogger(__name__)


@celery_app.task(
    name="chat.process_teams_message",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def process_teams_message_task(
    self,
    job_id: str,
    organisation_id: str,
):
    """
    Process Teams message and send response.

    This task:
    1. Loads the job and message data
    2. Processes through GeneralAgent (existing agent)
    3. Sends response back to Teams
    4. Updates the assistant message with response
    """
    logger.info(f"Processing Teams message job: {job_id}")

    try:
        service = TeamsProcessingService()
        service.process_message(
            job_id=UUID(job_id),
            organisation_id=UUID(organisation_id),
        )

        logger.info(f"Teams message job completed: {job_id}")

    except Exception as e:
        logger.error(f"Teams message job failed: {job_id}, error: {e}", exc_info=True)
        raise self.retry(exc=e)
```

### 5.10 Teams Processing Service

Create: `src/workers/chat_worker/services/teams_processing_service.py`

```python
"""Service for processing Teams messages in Celery worker."""

import logging
import asyncio
from uuid import UUID
from typing import Optional

from sqlmodel import select
from botbuilder.schema import ConversationReference, ChannelAccount, ConversationAccount

from src.domain.models.organisation_tables.chat_models import (
    ChatSession,
    ChatMessage,
    ChatMessageJob,
    JobStatus,
)
from src.infra.database.organisation_session import acquire_org_sync_session
from src.agents.workflows.general.agent import GeneralAgent
from src.agents.context import AgentContext
from src.service.teams.bot_service import TeamsBotService

logger = logging.getLogger(__name__)


class TeamsProcessingService:
    """
    Processes Teams messages using existing agent infrastructure.

    This is the sync version for Celery workers.
    """

    def process_message(
        self,
        job_id: UUID,
        organisation_id: UUID,
    ) -> None:
        """Process a Teams message job."""
        # Run async code in sync context
        asyncio.run(self._process_message_async(job_id, organisation_id))

    async def _process_message_async(
        self,
        job_id: UUID,
        organisation_id: UUID,
    ) -> None:
        """Async implementation of message processing."""

        # Load job data
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(ChatMessageJob).where(ChatMessageJob.job_id == job_id)
            result = await session.execute(stmt)
            job = result.scalar_one_or_none()

            if not job:
                logger.error(f"Job not found: {job_id}")
                return

            # Update job status
            job.status = JobStatus.IN_PROGRESS
            await session.commit()

            try:
                # Load messages
                metadata = job.job_metadata

                user_message_id = UUID(metadata["user_message_id"])
                assistant_message_id = UUID(metadata["assistant_message_id"])
                session_id = UUID(metadata["session_id"])
                user_id = UUID(metadata["user_id"]) if metadata.get("user_id") else None
                conversation_ref_data = metadata["conversation_reference"]

                # Get user message
                stmt = select(ChatMessage).where(ChatMessage.message_id == user_message_id)
                result = await session.execute(stmt)
                user_message = result.scalar_one()

                user_text = user_message.content.get("text", "")

                # Get chat session
                stmt = select(ChatSession).where(ChatSession.session_id == session_id)
                result = await session.execute(stmt)
                chat_session = result.scalar_one()

                # Build conversation history
                history = await self._build_history(session, session_id, limit=10)

            except Exception as e:
                job.status = JobStatus.FAILED
                job.error_message = str(e)
                await session.commit()
                raise

        # Process with agent (session released)
        try:
            response_text = await self._run_agent(
                organisation_id=organisation_id,
                user_id=user_id,
                user_text=user_text,
                history=history,
            )
        except Exception as e:
            logger.error(f"Agent error: {e}", exc_info=True)
            response_text = "Sorry, I encountered an error processing your request. Please try again."

        # Save response and send to Teams
        async for session in acquire_org_async_session(organisation_id):
            # Update assistant message
            stmt = select(ChatMessage).where(ChatMessage.message_id == assistant_message_id)
            result = await session.execute(stmt)
            assistant_message = result.scalar_one()

            assistant_message.content = {"text": response_text}
            assistant_message.extra = {
                **assistant_message.extra,
                "pending": False,
            }

            # Update job status
            stmt = select(ChatMessageJob).where(ChatMessageJob.job_id == job_id)
            result = await session.execute(stmt)
            job = result.scalar_one()
            job.status = JobStatus.COMPLETED

            await session.commit()

        # Send response to Teams
        await self._send_teams_response(conversation_ref_data, response_text)

    async def _run_agent(
        self,
        organisation_id: UUID,
        user_id: Optional[UUID],
        user_text: str,
        history: list[dict],
    ) -> str:
        """Run the GeneralAgent to get response."""

        # Create agent context (uses fresh session pattern)
        context = AgentContext(
            organisation_id=organisation_id,
            deal_id=None,  # No deal context for general chat
            org_name="",  # Will be loaded by agent if needed
            user_id=user_id,
            metadata={"channel": "teams"},
        )

        # Create and run agent
        agent = GeneralAgent(context=context)

        # Build messages for agent
        messages = []
        for msg in history:
            messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })
        messages.append({
            "role": "user",
            "content": user_text,
        })

        # Run agent (non-streaming for Teams)
        response = await agent.run(messages=messages)

        return response.content if response else "I couldn't generate a response."

    async def _build_history(
        self,
        session,
        session_id: UUID,
        limit: int = 10,
    ) -> list[dict]:
        """Build conversation history for context."""
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.session_id == session_id,
                ChatMessage.role.in_(["user", "assistant"]),
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        messages = result.scalars().all()

        history = []
        for msg in reversed(messages):  # Oldest first
            content = msg.content.get("text", "") if isinstance(msg.content, dict) else str(msg.content)
            if content:
                history.append({
                    "role": msg.role,
                    "content": content,
                })

        return history

    async def _send_teams_response(
        self,
        conversation_ref_data: dict,
        message: str,
    ) -> None:
        """Send response back to Teams."""

        # Reconstruct conversation reference
        conversation_ref = ConversationReference(
            activity_id=conversation_ref_data.get("activity_id"),
            user=ChannelAccount(id=conversation_ref_data.get("user_id")),
            bot=ChannelAccount(id=conversation_ref_data.get("bot_id")),
            conversation=ConversationAccount(
                id=conversation_ref_data.get("conversation_id"),
                name=conversation_ref_data.get("conversation_name"),
            ),
            channel_id=conversation_ref_data.get("channel_id"),
            service_url=conversation_ref_data.get("service_url"),
        )

        # Send via bot service
        bot_service = TeamsBotService()
        await bot_service.send_proactive_message(
            conversation_reference=conversation_ref.__dict__,
            message=message,
        )

        logger.info("Teams response sent successfully")
```

### 5.11 Register Router

Update: `src/api/routers/__init__.py` or your main router setup:

```python
from src.api.routers.teams.webhook_handler import router as teams_router
from src.api.routers.teams.auth_handler import router as teams_auth_router

# Add to your FastAPI app
app.include_router(teams_router)
app.include_router(teams_auth_router)
```

---

## 6. Authentication Flow

### 6.1 Complete Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           AUTHENTICATION FLOW                                │
└─────────────────────────────────────────────────────────────────────────────┘

1. USER SENDS MESSAGE (Not Authenticated)
   ┌─────────┐         ┌─────────────┐         ┌──────────────┐
   │  Teams  │ ──────▶ │ Azure Bot   │ ──────▶ │ Your Backend │
   │  User   │         │ Service     │         │              │
   └─────────┘         └─────────────┘         └──────────────┘
                                                      │
                                               Check TeamsUserMapping
                                               (not found or not authenticated)
                                                      │
                                                      ▼
2. BOT SENDS LOGIN CARD
   ┌─────────┐         ┌─────────────┐         ┌──────────────┐
   │  Teams  │ ◀────── │ Azure Bot   │ ◀────── │ Your Backend │
   │  User   │ Shows   │ Service     │         │ Login Card   │
   └─────────┘ Card    └─────────────┘         └──────────────┘


3. USER CLICKS LOGIN BUTTON
   ┌─────────┐                                  ┌──────────────┐
   │  Teams  │ ─────────────────────────────▶  │ Your Login   │
   │  User   │ Opens browser to your login     │ Page         │
   └─────────┘ page with state token           └──────────────┘
                                                      │
                                               User enters credentials
                                                      │
                                                      ▼
4. USER AUTHENTICATES WITH YOUR SYSTEM
   ┌─────────┐                                  ┌──────────────┐
   │ Browser │ ─────────────────────────────▶  │ Your Auth    │
   │         │ POST /auth/login                │ Endpoint     │
   └─────────┘ (email + password)              └──────────────┘
                                                      │
                                               Validate credentials
                                               Create session
                                                      │
                                                      ▼
5. REDIRECT TO TEAMS CALLBACK
   ┌─────────┐                                  ┌──────────────┐
   │ Browser │ ◀─────────────────────────────  │ Your Auth    │
   │         │ Redirect to /api/teams/auth/    │ Endpoint     │
   └─────────┘ callback?state=xxx              └──────────────┘
                    │
                    ▼
6. CALLBACK LINKS TEAMS USER TO YOUR USER
   ┌─────────┐                                  ┌──────────────┐
   │ Browser │ ─────────────────────────────▶  │ Teams Auth   │
   │         │ GET /api/teams/auth/callback    │ Callback     │
   └─────────┘ (with session cookie)           └──────────────┘
                                                      │
                                               - Decode state token
                                               - Get current user from session
                                               - Create TeamsUserMapping
                                               - Link teams_user_id → user_id
                                                      │
                                                      ▼
7. SUCCESS PAGE + NOTIFICATION TO TEAMS
   ┌─────────┐                                  ┌──────────────┐
   │ Browser │ ◀─────────────────────────────  │ Success Page │
   │         │ "You can close this window"     └──────────────┘
   └─────────┘
                                                      │
   ┌─────────┐         ┌─────────────┐               │
   │  Teams  │ ◀────── │ Azure Bot   │ ◀─────────────┘
   │  User   │ "You    │ Service     │ Proactive message
   └─────────┘ are     └─────────────┘
              signed in!"


8. SUBSEQUENT MESSAGES (Authenticated)
   ┌─────────┐         ┌─────────────┐         ┌──────────────┐
   │  Teams  │ ──────▶ │ Azure Bot   │ ──────▶ │ Your Backend │
   │  User   │ "What   │ Service     │         │              │
   └─────────┘ is..."  └─────────────┘         └──────────────┘
                                                      │
                                               Check TeamsUserMapping
                                               (found AND authenticated)
                                                      │
                                               Process with GeneralAgent
                                                      │
                                                      ▼
   ┌─────────┐         ┌─────────────┐         ┌──────────────┐
   │  Teams  │ ◀────── │ Azure Bot   │ ◀────── │ AI Response  │
   │  User   │ Answer  │ Service     │         │              │
   └─────────┘         └─────────────┘         └──────────────┘
```

### 6.2 Your Login Page Requirements

Your existing login page needs a small modification to handle the Teams flow:

```python
# In your login endpoint, after successful authentication:

@router.post("/auth/login")
async def login(request: LoginRequest, response: Response):
    # ... your existing login logic ...

    # Check if this is a Teams login (has state parameter)
    if request.state and request.channel == "teams":
        # Redirect to Teams callback instead of normal dashboard
        callback_url = f"/api/teams/auth/callback?state={request.state}"
        return RedirectResponse(url=callback_url, status_code=302)

    # Normal login flow
    return {"status": "success", "token": token}
```

---

## 7. Message Processing Pipeline

### 7.1 How Messages Flow Through Your System

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        MESSAGE PROCESSING PIPELINE                           │
└─────────────────────────────────────────────────────────────────────────────┘

1. INCOMING MESSAGE
   Teams → Azure Bot → POST /api/teams/messages

2. WEBHOOK HANDLER (src/api/routers/teams/webhook_handler.py)
   │
   ├─ Validate Bot Framework auth header
   ├─ Parse Activity
   └─ Call TeamsBotService.process_activity()

3. BOT SERVICE (src/service/teams/bot_service.py)
   │
   ├─ Handle activity type (message, conversationUpdate, etc.)
   ├─ For messages:
   │   ├─ Check authentication (TeamsUserMapping)
   │   ├─ If not authenticated → Send login card
   │   └─ If authenticated → Call TeamsMessageService
   └─ Send typing indicator

4. MESSAGE SERVICE (src/service/teams/teams_message_service.py)
   │
   ├─ Get/create ChatSession (channel = 'teams')
   ├─ Create ChatMessage (user message)
   ├─ Create ChatMessage (assistant placeholder)
   ├─ Create ChatMessageJob
   └─ Enqueue to Celery: chat.process_teams_message

5. CELERY WORKER (src/workers/chat_worker/tasks/teams_response.py)
   │
   ├─ Load job metadata
   ├─ Build conversation history
   └─ Call TeamsProcessingService

6. PROCESSING SERVICE (src/workers/chat_worker/services/teams_processing_service.py)
   │
   ├─ Create AgentContext (organisation_id, user_id)
   ├─ Run GeneralAgent
   │   ├─ Agent queries knowledge base
   │   ├─ Agent may use other tools
   │   └─ Agent generates response
   ├─ Update ChatMessage (assistant response)
   ├─ Update ChatMessageJob (completed)
   └─ Call TeamsBotService.send_proactive_message()

7. RESPONSE SENT TO TEAMS
   Your Backend → Azure Bot → Teams
```

### 7.2 Reuse Points

| Component | Reused From | Notes |
|-----------|-------------|-------|
| `ChatSession` | Existing model | Add `channel` field |
| `ChatMessage` | Existing model | Same structure |
| `ChatMessageJob` | Existing model | New job_type |
| `GeneralAgent` | Existing agent | No changes needed |
| `chat_worker` | Existing worker | Add new task |
| Knowledge search | Existing tool | No changes needed |

---

## 8. Multi-Tenant Distribution

### 8.1 How Users from Different Organizations Install Your Bot

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         DISTRIBUTION OPTIONS                                 │
└─────────────────────────────────────────────────────────────────────────────┘

OPTION 1: Microsoft AppSource (Recommended for Public Distribution)
───────────────────────────────────────────────────────────────────

1. Submit your Teams app package to Partner Center
2. Microsoft reviews and approves
3. App appears in Teams App Store
4. Any organization can install

   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
   │   Org A     │     │   Org B     │     │   Org C     │
   │   Admin     │     │   Admin     │     │   Admin     │
   └──────┬──────┘     └──────┬──────┘     └──────┬──────┘
          │                   │                   │
          ▼                   ▼                   ▼
   ┌─────────────────────────────────────────────────────┐
   │              Microsoft Teams App Store              │
   │                                                     │
   │     [CopilotGTM Bot]  ⭐⭐⭐⭐☆  [Install]        │
   └─────────────────────────────────────────────────────┘
          │                   │                   │
          ▼                   ▼                   ▼
   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
   │  Installed  │     │  Installed  │     │  Installed  │
   │  in Org A   │     │  in Org B   │     │  in Org C   │
   └─────────────┘     └─────────────┘     └─────────────┘


OPTION 2: Direct Upload (For Known Customers)
─────────────────────────────────────────────

1. Send your Teams app package (.zip) to customer
2. Customer's Teams admin uploads to their org
3. No Microsoft review needed

   You ──▶ Send .zip ──▶ Customer Admin ──▶ Upload to Teams ──▶ Available in Org


OPTION 3: Admin-Managed Deployment
──────────────────────────────────

1. Publish app to your organization catalog
2. Share with specific external organizations
3. Their admins can approve installation

```

### 8.2 AppSource Submission Process

1. **Prepare Your App**
   - Ensure manifest.json is complete
   - Test thoroughly
   - Prepare privacy policy and terms of service URLs

2. **Create Partner Center Account**
   - Go to https://partner.microsoft.com
   - Register as a developer

3. **Submit for Review**
   - Upload your .zip package
   - Fill in app metadata
   - Submit for validation

4. **Review Process**
   - Microsoft validates functionality
   - Security review
   - Policy compliance check
   - Takes 2-4 weeks typically

5. **Publication**
   - Once approved, app appears in Teams Store
   - Available to all Microsoft 365 organizations

---

## 9. Testing & Debugging

### 9.1 Local Testing Setup

**1. Use ngrok or similar for HTTPS tunnel:**

```bash
# Install ngrok
npm install -g ngrok

# Start tunnel
ngrok http 8000
```

**2. Update Azure Bot messaging endpoint:**
```
https://abc123.ngrok.io/api/teams/messages
```

**3. Sideload the app in Teams:**
- Open Teams
- Go to Apps → Manage your apps → Upload a custom app
- Select your .zip package
- Test in a chat

### 9.2 Testing Checklist

```
[ ] Bot receives messages from Teams
[ ] Login card displays correctly
[ ] Login URL works and redirects properly
[ ] Authentication callback links users correctly
[ ] Authenticated messages are processed
[ ] Responses appear in Teams
[ ] Logout works
[ ] Status command shows correct state
[ ] Help command displays properly
[ ] Conversation history is maintained
[ ] Multi-org isolation works (users only see their org's data)
```

### 9.3 Debugging Tips

**Check logs:**
```bash
# API logs
tail -f logs/api.log | grep -i teams

# Worker logs
tail -f logs/chat_worker.log | grep -i teams
```

**Test Bot Framework auth:**
```python
# Verify incoming requests are from Bot Framework
# The adapter handles this automatically, but you can log:
logger.debug(f"Auth header: {auth_header[:50]}...")
```

**Test database:**
```python
# Check teams_user_mappings
SELECT * FROM teams_user_mappings WHERE teams_user_id = 'xxx';

# Check chat sessions
SELECT * FROM chat_sessions WHERE channel = 'teams';
```

---

## 10. Production Deployment

### 10.1 Environment Variables

```env
# Production .env

# Teams Bot Configuration
TEAMS_ENABLED=true
TEAMS_BOT_APP_ID=your-production-app-id
TEAMS_BOT_APP_SECRET=your-production-secret
TEAMS_BOT_TENANT_ID=your-azure-tenant-id

# Auth URLs (your production domain)
TEAMS_AUTH_LOGIN_URL=https://app.yourcompany.com/login
TEAMS_AUTH_CALLBACK_URL=https://app.yourcompany.com/api/teams/auth/callback
```

### 10.2 Azure Bot Service Configuration

1. **Messaging Endpoint**: `https://api.yourcompany.com/api/teams/messages`
2. **Enable Teams Channel**: Already done in setup
3. **Scaling**: Azure Bot Service handles routing automatically

### 10.3 Security Checklist

```
[ ] Bot credentials stored in Azure Key Vault
[ ] HTTPS everywhere
[ ] Rate limiting on webhook endpoint
[ ] Validate Bot Framework auth headers
[ ] Organization isolation in all queries
[ ] Token expiration handling
[ ] Input sanitization
[ ] Logging without sensitive data
```

### 10.4 Monitoring

```python
# Add metrics for Teams integration

# Track message counts
metrics.increment("teams.messages.received")
metrics.increment("teams.messages.processed")
metrics.increment("teams.auth.login_started")
metrics.increment("teams.auth.login_completed")

# Track errors
metrics.increment("teams.errors", tags={"type": error_type})

# Track latency
metrics.histogram("teams.response_time", duration_ms)
```

### 10.5 Database Indexes

Ensure these indexes exist for performance:

```sql
-- Fast lookup by Teams user
CREATE INDEX ix_teams_user_mappings_lookup
ON teams_user_mappings (teams_user_id, teams_tenant_id, organisation_id);

-- Fast session lookup by channel
CREATE INDEX ix_chat_sessions_channel
ON chat_sessions (channel, organisation_id)
WHERE is_deleted = false;

-- Channel metadata lookup (JSONB)
CREATE INDEX ix_chat_sessions_teams_conv
ON chat_sessions ((channel_metadata->>'teams_conversation_id'))
WHERE channel = 'teams';
```

---

## Appendix A: Complete File Structure

```
src/
├── api/
│   └── routers/
│       └── teams/
│           ├── __init__.py
│           ├── webhook_handler.py       # POST /api/teams/messages
│           ├── auth_handler.py          # GET /api/teams/auth/callback
│           └── schemas.py               # Pydantic schemas
├── service/
│   └── teams/
│       ├── __init__.py
│       ├── bot_service.py               # TeamsBotService
│       ├── teams_auth_service.py        # TeamsAuthService
│       └── teams_message_service.py     # TeamsMessageService
├── domain/
│   └── models/
│       └── organisation_tables/
│           └── teams_models.py          # TeamsUserMapping
├── infra/
│   └── config/
│       └── teams_config.py              # TeamsConfig
└── workers/
    └── chat_worker/
        ├── tasks/
        │   └── teams_response.py        # Celery task
        └── services/
            └── teams_processing_service.py

migrations/
└── org_migrations/
    └── versions/
        ├── xxx_add_teams_user_mappings.py
        └── yyy_add_channel_to_chat_sessions.py

# Config files
.env                                      # TEAMS_* variables
```

---

## Appendix B: Quick Reference

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/teams/messages` | POST | Webhook for Teams messages |
| `/api/teams/auth/callback` | GET | Auth callback after login |
| `/api/teams/health` | GET | Health check |

### Bot Commands

| Command | Description |
|---------|-------------|
| `login` | Sign in to your account |
| `logout` | Sign out |
| `status` | Check authentication status |
| `help` | Show help |

### Database Tables

| Table | Schema | Purpose |
|-------|--------|---------|
| `teams_user_mappings` | org_* | Maps Teams users to app users |
| `chat_sessions` | org_* | Stores conversations (add `channel` field) |
| `chat_messages` | org_* | Stores messages (existing) |
| `chat_message_jobs` | org_* | Job tracking (existing) |

---

## Summary

This integration:

1. **Reuses your existing infrastructure** - Chat models, agents, workers, and knowledge base
2. **Adds minimal new components** - One new table, one new field, new service layer
3. **Supports multi-tenant distribution** - Users from any org can install via AppSource
4. **Uses your custom auth** - Not dependent on Microsoft login
5. **Maintains organization isolation** - Each user's data stays within their org
