# Read-Only Chat Session Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a chat owner grant selected users in the same organisation read-only access to the original chat, including future committed messages and linked audio attachments.

**Architecture:** Keep `ChatSession.user_id` as the single owner and add a normalized `chat_session_access` junction table. Route reads through one owner-or-viewer resolver and writes through one owner-only resolver; viewers refresh canonical PostgreSQL messages and never consume owner job streams or cloned data.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLAlchemy async sessions, PostgreSQL/Alembic organisation migrations, Pydantic v2, pytest, Ruff.

**Source design:** `docs/Technical Documentation/chat_bots/chat-session-sharing-architecture.md`

---

## Scope

Included: same-organisation viewer validation, idempotent grant/revoke, owner-only viewer enumeration, “Shared with me,” shared transcript/audio reads, future-message refresh, and owner-only mutations, voice writes, and job SSE.

Excluded: cloned chats, editor roles, organisation/team/public sharing, expiring links, raw realtime recordings, new viewer SSE/WebSockets, and frontend implementation.

## File Map

**Create:**

- `src/domain/models/organisation_tables/chat_session_access_models.py`
- `src/service/chat/chat_access.py`
- `migrations/org_migrations/versions/3c7f79637720_079_chat_session_access.py`
- `tests/migrations/org/test_079_chat_session_access.py`
- `tests/service/chat/test_chat_access.py`
- `tests/service/chat/test_chat_session_sharing_service.py`
- `tests/api/routers/chat/test_chat_sharing_handler.py`
- `tests/service/chat/test_chat_message_sse_service.py`

**Modify:**

- `src/domain/models/organisation_tables/__init__.py`
- `src/service/chat/chat_session_service.py`
- `src/service/chat/chat_message_service.py`
- `src/service/chat/chat_message_sse_service.py`
- `src/api/schemas/chat_models.py`
- `src/api/routers/chat/sessions_handler.py`
- `src/api/routers/chat/messages_handler.py`
- `tests/api/routers/chat/conftest.py`
- existing chat session, message, security-boundary, route, and voice persistence tests
- `docs/Technical Documentation/chat_bots/chat-session-sharing-architecture.md`

---

### Task 1: Add The Access Table And Model

**Files:**
- Create: `migrations/org_migrations/versions/3c7f79637720_079_chat_session_access.py`
- Create: `tests/migrations/org/test_079_chat_session_access.py`
- Create: `src/domain/models/organisation_tables/chat_session_access_models.py`
- Modify: `src/domain/models/organisation_tables/__init__.py`

- [ ] **Step 1: Write the failing migration contract**

```python
from pathlib import Path

MIGRATION_PATH = Path(
    "migrations/org_migrations/versions/3c7f79637720_079_chat_session_access.py"
)


def test_chat_session_access_contract() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8").lower()
    assert 'revision: str = "3c7f79637720"' in source
    assert 'down_revision: str | none = "3c7f79637721"' in source
    assert '"chat_session_access"' in source
    assert '"pk_chat_session_access"' in source
    assert 'ondelete="cascade"' in source
    assert '"ix_chat_session_access_viewer_chats"' in source
    assert '["organisation_id", "viewer_user_id", "created_at"]' in source
```

- [ ] **Step 2: Prove RED**

Run: `uv run pytest tests/migrations/org/test_079_chat_session_access.py -q`

Expected: FAIL because the migration file is missing.

- [ ] **Step 3: Create migration 079**

Use revision `3c7f79637720`, down revision `3c7f79637721`, and this DDL body:

```python
def upgrade() -> None:
    op.create_table(
        "chat_session_access",
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("viewer_user_id", sa.UUID(), nullable=False),
        sa.Column("granted_by_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.session_id"],
            name="fk_chat_session_access_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "session_id", "viewer_user_id", name="pk_chat_session_access"
        ),
    )
    op.create_index(
        "ix_chat_session_access_viewer_chats",
        "chat_session_access",
        ["organisation_id", "viewer_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_session_access_viewer_chats",
        table_name="chat_session_access",
    )
    op.drop_table("chat_session_access")
```

- [ ] **Step 4: Create and export the model**

```python
from uuid import UUID

from sqlalchemy import Index
from sqlmodel import Field

from .base_mixins import CreatedAtMixin


class ChatSessionAccess(CreatedAtMixin, table=True):
    """Grant one user read-only access to one chat session."""

    __tablename__ = "chat_session_access"
    __table_args__ = (
        Index(
            "ix_chat_session_access_viewer_chats",
            "organisation_id",
            "viewer_user_id",
            "created_at",
        ),
        {"comment": "Explicit read-only viewers for chat sessions."},
    )

    organisation_id: UUID = Field(nullable=False)
    session_id: UUID = Field(
        foreign_key="chat_sessions.session_id", primary_key=True
    )
    viewer_user_id: UUID = Field(primary_key=True)
    granted_by_user_id: UUID = Field(nullable=False)
```

Export `ChatSessionAccess` from `organisation_tables/__init__.py` and add it to `__all__`.

- [ ] **Step 5: Prove GREEN**

Run:

```powershell
uv run pytest tests/migrations/org/test_079_chat_session_access.py tests/migrations/test_org_migration_head_contract.py -q
uv run python -c "from src.domain.models.organisation_tables import ChatSessionAccess; assert [c.name for c in ChatSessionAccess.__table__.primary_key.columns] == ['session_id', 'viewer_user_id']"
```

Expected: both exit 0.

- [ ] **Step 6: Commit**

```powershell
git add migrations/org_migrations/versions/3c7f79637720_079_chat_session_access.py tests/migrations/org/test_079_chat_session_access.py src/domain/models/organisation_tables/chat_session_access_models.py src/domain/models/organisation_tables/__init__.py
git commit -m "feat: add chat session access table"
```

---

### Task 2: Centralize Chat Authorization

**Files:**
- Create: `src/service/chat/chat_access.py`
- Create: `tests/service/chat/test_chat_access.py`
- Modify: `src/service/chat/chat_session_service.py`
- Modify: `src/service/chat/chat_message_service.py`

- [ ] **Step 1: Write failing resolver tests**

Test that readable SQL includes organisation, non-deleted session, owner OR `EXISTS chat_session_access`, while owned SQL includes organisation, non-deleted session, and owner without the access table.

```python
@pytest.mark.asyncio
async def test_readable_resolver_uses_owner_or_explicit_grant() -> None:
    db = AsyncMock()
    row = MagicMock(spec=ChatSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute.return_value = result

    resolved = await resolve_readable_chat_session(
        db,
        organisation_id=uuid4(),
        user_id=uuid4(),
        session_id=uuid4(),
    )

    sql = str(db.execute.await_args.args[0]).lower()
    assert resolved is row
    assert "chat_sessions.organisation_id" in sql
    assert "chat_sessions.is_deleted" in sql
    assert "chat_sessions.user_id" in sql
    assert "exists" in sql
    assert "chat_session_access.viewer_user_id" in sql
```

- [ ] **Step 2: Prove RED**

Run: `uv run pytest tests/service/chat/test_chat_access.py -q`

Expected: FAIL because `chat_access.py` is missing.

- [ ] **Step 3: Implement the shared helpers**

```python
async def resolve_owned_chat_session(
    db: AsyncSession,
    *,
    organisation_id: UUID,
    user_id: UUID,
    session_id: UUID,
) -> ChatSession:
    result = await db.execute(
        select(ChatSession).where(
            col(ChatSession.session_id) == session_id,
            col(ChatSession.organisation_id) == organisation_id,
            col(ChatSession.user_id) == user_id,
            col(ChatSession.is_deleted).is_(False),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError("Chat session not found")
    return session


async def resolve_readable_chat_session(
    db: AsyncSession,
    *,
    organisation_id: UUID,
    user_id: UUID,
    session_id: UUID,
) -> ChatSession:
    grant_exists = exists(
        select(ChatSessionAccess.session_id).where(
            col(ChatSessionAccess.organisation_id) == organisation_id,
            col(ChatSessionAccess.session_id) == session_id,
            col(ChatSessionAccess.viewer_user_id) == user_id,
        )
    )
    result = await db.execute(
        select(ChatSession).where(
            col(ChatSession.session_id) == session_id,
            col(ChatSession.organisation_id) == organisation_id,
            col(ChatSession.is_deleted).is_(False),
            or_(col(ChatSession.user_id) == user_id, grant_exists),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError("Chat session not found")
    return session
```

- [ ] **Step 4: Reuse owner resolution**

Replace duplicated owner lookups in `delete_session`, `rename_session`, `set_favorite`, and `create_user_message`. Do not switch message reads yet; Task 6 changes read and attachment authorization atomically.

- [ ] **Step 5: Prove GREEN**

Run:

```powershell
uv run pytest tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py -q
```

Expected: PASS; mutation-boundary SQL still contains organisation, user, and deletion filters.

- [ ] **Step 6: Commit**

```powershell
git add src/service/chat/chat_access.py src/service/chat/chat_session_service.py src/service/chat/chat_message_service.py tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py
git commit -m "refactor: centralize chat session authorization"
```

---

### Task 3: Implement Grant, Revoke, And Viewer Listing

**Files:**
- Modify: `src/service/chat/chat_session_service.py`
- Create: `tests/service/chat/test_chat_session_sharing_service.py`

- [ ] **Step 1: Write failing tests**

Add tests named:

- `test_grant_rejects_owner`
- `test_grant_requires_active_approved_same_org_user`
- `test_non_owner_cannot_grant`
- `test_duplicate_grant_converges_on_one_row`
- `test_revoke_is_idempotent`
- `test_non_owner_cannot_revoke`
- `test_only_owner_can_list_viewers`
- `test_viewer_list_is_scoped_to_org_and_session`

Representative assertion:

```python
with pytest.raises(ValueError, match="owner already has access"):
    await service.grant_viewer(
        session_id=session.session_id,
        viewer_user_id=session.user_id,
    )
```

- [ ] **Step 2: Prove RED**

Run: `uv run pytest tests/service/chat/test_chat_session_sharing_service.py -q`

Expected: FAIL because the methods are absent.

- [ ] **Step 3: Implement exact service contracts**

```python
async def grant_viewer(
    self, *, session_id: UUID, viewer_user_id: UUID
) -> ChatSessionAccess:
```

```python
async def revoke_viewer(
    self, *, session_id: UUID, viewer_user_id: UUID
) -> None:
```

```python
async def list_viewers(
    self, *, session_id: UUID
) -> list[ChatSessionAccess]:
```

`grant_viewer()` resolves owner access, rejects the owner, and selects the target `User` using exact user ID, organisation ID, `SupabaseUserStatus.ACTIVE`, and `AdminUserStatus.APPROVED`. Insert idempotently:

```python
statement = (
    pg_insert(ChatSessionAccess)
    .values(
        organisation_id=self.organisation_id,
        session_id=session_id,
        viewer_user_id=viewer_user_id,
        granted_by_user_id=self.user_id,
    )
    .on_conflict_do_nothing(index_elements=["session_id", "viewer_user_id"])
)
await self.db.execute(statement)
await self.db.commit()
```

Then select and return the effective row. `revoke_viewer()` resolves the owner, deletes the exact org/session/viewer row, and commits even when absent. `list_viewers()` resolves the owner and orders by `created_at`, then viewer ID.

- [ ] **Step 4: Prove GREEN**

Run:

```powershell
uv run pytest tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_security_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/service/chat/chat_session_service.py tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_security_boundaries.py
git commit -m "feat: manage read-only chat viewers"
```

---

### Task 4: Expose Share Management APIs

**Files:**
- Modify: `src/api/schemas/chat_models.py`
- Modify: `src/api/routers/chat/sessions_handler.py`
- Modify: `tests/api/routers/chat/conftest.py`
- Create: `tests/api/routers/chat/test_chat_sharing_handler.py`

- [ ] **Step 1: Write failing route tests**

Test:

```python
grant = client.put(f"/users/chats/{session_id}/shares/{viewer_id}")
assert grant.status_code == 200
assert grant.json()["data"]["viewer_user_id"] == str(viewer_id)

listed = client.get(f"/users/chats/{session_id}/shares")
assert listed.status_code == 200
assert listed.json()["data"][0]["viewer_user_id"] == str(viewer_id)

revoked = client.delete(f"/users/chats/{session_id}/shares/{viewer_id}")
assert revoked.status_code == 204
```

Also cover owner-as-viewer `400`, inaccessible session `404`, invalid target `404`, and duplicate PUT success.

- [ ] **Step 2: Prove RED**

Run: `uv run pytest tests/api/routers/chat/test_chat_sharing_handler.py -q`

Expected: FAIL with route 404s.

- [ ] **Step 3: Add response model**

```python
class ChatSessionShareOut(BaseModel):
    session_id: UUID
    viewer_user_id: UUID
    granted_by_user_id: UUID
    created_at: datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Add routes**

Under the existing `/users/chats` prefix add:

```text
PUT    /{session_id}/shares/{viewer_user_id}
GET    /{session_id}/shares
DELETE /{session_id}/shares/{viewer_user_id}
```

PUT returns `GenericResponse[ChatSessionShareOut]`; GET returns `GenericResponse[list[ChatSessionShareOut]]`; DELETE returns empty `204`. Map owner-self validation to `400`; map missing session/user and non-owner management to the same non-enumerating `404` message.

- [ ] **Step 5: Extend route fakes**

Make `dependency_overrides` yield its `FakeChatSessionService`. Add only the three matching fake methods and a `create_owned_session()` seeding helper; use `SimpleNamespace` rows with the four response fields.

- [ ] **Step 6: Prove GREEN**

Run:

```powershell
uv run pytest tests/api/routers/chat/test_chat_sharing_handler.py tests/api/routers/chat/test_chat_sessions_handler.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/api/schemas/chat_models.py src/api/routers/chat/sessions_handler.py tests/api/routers/chat/conftest.py tests/api/routers/chat/test_chat_sharing_handler.py
git commit -m "feat: expose chat sharing endpoints"
```

---

### Task 5: Add Unified Authorized Listing And Capabilities

**Files:**
- Modify: `src/service/chat/chat_session_service.py`
- Modify: `src/api/schemas/chat_models.py`
- Modify: `src/api/routers/chat/sessions_handler.py`
- Modify: sharing service/route tests and `tests/api/routers/chat/conftest.py`

- [ ] **Step 1: Write failing tests**

Prove the default list returns owned or explicitly shared sessions through one
authorization-aware query, while `scope=owned|shared` filters the same collection.
It excludes deleted and `TEAMS_CHAT` sessions, orders by `(updated_at, session_id)` descending, and paginates stably. Prove viewer output:

```python
assert shared_chat["access"] == "viewer"
assert shared_chat["capabilities"] == {
    "read": True,
    "send": False,
    "rename": False,
    "delete": False,
    "manage_shares": False,
}
```

- [ ] **Step 2: Prove RED**

Run: `uv run pytest tests/service/chat/test_chat_session_sharing_service.py tests/api/routers/chat/test_chat_sharing_handler.py -q`

Expected: FAIL because unified authorized listing/capabilities are missing.

- [ ] **Step 3: Add schemas**

```python
class ChatSessionCapabilities(BaseModel):
    read: bool
    send: bool
    rename: bool
    delete: bool
    manage_shares: bool
```

Add `access: Literal["owner", "viewer"] = "owner"` and `capabilities: ChatSessionCapabilities` to `ChatSessionOut`. Update `_to_out(chat, *, access="owner")`; it derives all booleans from the one access value.

- [ ] **Step 4: Extend the existing list service and route**

Extend `list_sessions` instead of creating a second collection:

```python
async def list_sessions(
    self,
    *,
    limit: int = 20,
    cursor: str | None = None,
    scope: Literal["all", "owned", "shared"] = "all",
) -> tuple[list[ChatSession], str | None]:
```

Keep `GET /users/chats/list_chats` as the single collection endpoint. Its default
`scope=all` applies `owner OR EXISTS(explicit grant)` before the canonical
`(updated_at, session_id)` ordering and cursor. `scope=owned` and `scope=shared`
filter the same collection without changing its response shape. Derive each row's
`access` and `capabilities` from ownership. Keep `/users/chats/shared` as a separate,
read-only settings projection when sharer attribution is required; do not duplicate stored
chat metadata per viewer.

- [ ] **Step 5: Prove GREEN**

Run:

```powershell
uv run pytest tests/service/chat/test_chat_session_service.py tests/service/chat/test_chat_session_sharing_service.py tests/api/routers/chat/test_chat_sessions_handler.py tests/api/routers/chat/test_chat_sharing_handler.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/service/chat/chat_session_service.py src/api/schemas/chat_models.py src/api/routers/chat/sessions_handler.py tests/service/chat/test_chat_session_sharing_service.py tests/api/routers/chat/conftest.py tests/api/routers/chat/test_chat_sharing_handler.py
git commit -m "feat: list chats shared with the current user"
```

---

### Task 6: Allow Shared Transcript And Audio Reads

**Files:**
- Modify: `src/service/chat/chat_message_service.py`
- Modify: `src/api/routers/chat/messages_handler.py`
- Modify: message service, security-boundary, route tests, and route fake

- [ ] **Step 1: Write failing tests**

Add exact cases:

- selected viewer lists owner messages;
- unselected user cannot list them;
- viewer cannot create a message or change feedback;
- viewer gets an audio URL only through an authorized session/message/link;
- another unlinked owner attachment remains unavailable;
- revocation blocks new attachment URL generation; and
- `test_refreshing_latest_page_reveals_new_owner_message_without_copying`: listing the same session again after an owner commit reveals the new row with the original `session_id` unchanged.

- [ ] **Step 2: Prove RED**

Run:

```powershell
uv run pytest tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py -q
```

Expected: FAIL because reads still require owner identity.

- [ ] **Step 3: Switch history reads**

At the start of `list_messages()` call `resolve_readable_chat_session(...)`. Keep `create_user_message()` and `update_message_feedback()` owner-only.

- [ ] **Step 4: Scope attachment reads through the session**

Change the signature:

```python
async def get_message_attachments(
    self,
    *,
    session_id: UUID,
    messages: list[ChatMessage],
) -> dict[UUID, list[Attachment]]:
```

Resolve readable session access, join link → message → attachment, and require message session, requested message IDs, attachment organisation, and non-deleted attachment. Remove `Attachment.user_id == viewer` only from this read query; keep uploader checks on upload and `_resolve_attachments()`.

- [ ] **Step 5: Update handler and fake**

```python
atts_by_msg = await service.get_message_attachments(
    session_id=session_id,
    messages=msgs,
)
```

Update the fake to the same keyword-only signature. Keep the current SAS duration; revocation blocks generation of new URLs.

- [ ] **Step 6: Update mutation-resistant assertions**

Writes must still require owner user ID. Reads must require organisation, non-deleted session, and owner-or-grant. Attachment reads must require authorised session, message/session linkage, organisation, and non-deleted attachment without viewer ownership of the blob.

- [ ] **Step 7: Prove GREEN**

Run:

```powershell
uv run pytest tests/service/chat/test_chat_access.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/api/routers/chat/test_chat_messages_handler.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add src/service/chat/chat_message_service.py src/api/routers/chat/messages_handler.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/api/routers/chat/conftest.py tests/api/routers/chat/test_chat_messages_handler.py
git commit -m "feat: allow viewers to read shared chat history"
```

---

### Task 7: Lock Down Job SSE And Voice Writes

**Files:**
- Modify: `src/service/chat/chat_message_sse_service.py`
- Modify: `src/api/routers/chat/messages_handler.py`
- Create: `tests/service/chat/test_chat_message_sse_service.py`
- Modify: message route and voice persistence tests

- [ ] **Step 1: Write failing SSE tests**

```python
@pytest.mark.asyncio
async def test_can_stream_rejects_non_owner(monkeypatch) -> None:
    service = ChatMessageSSEService(uuid4(), uuid4(), uuid4())
    job = MagicMock(user_id=uuid4(), status=JobStatus.PENDING)
    session = AsyncMock()
    session.get.return_value = job

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(sse_module, "acquire_org_async_session", sessions)
    assert await service.can_stream() is False
```

Add matching-owner, missing-job, and route-level 404 tests.

- [ ] **Step 2: Add voice invariant test**

Compile `_get_owned_voice_session()` SQL and assert session ID, organisation ID, user ID, `VOICE_CHAT`, and `is_deleted` appear in its WHERE clause. A viewer still receives `VoiceTurnPersistenceServiceError` with 404.

- [ ] **Step 3: Prove RED**

Run:

```powershell
uv run pytest tests/service/chat/test_chat_message_sse_service.py tests/service/voice_realtime/test_turn_persistence_service.py -q
```

Expected: SSE tests FAIL because `can_stream()` is absent; voice guard remains green.

- [ ] **Step 4: Implement job ownership lookup**

```python
async def _owned_job_status(self) -> JobStatus | None:
    async for session in acquire_org_async_session(UUID(self.organisation_id)):
        job = await session.get(ChatMessageJob, UUID(self.job_id))
        if job is None or str(job.user_id) != self.user_id:
            return None
        return job.status
    return None


async def can_stream(self) -> bool:
    return await self._owned_job_status() is not None
```

Make `_is_job_completed()` compare this result with `JobStatus.COMPLETED`.

- [ ] **Step 5: Reject before opening SSE**

```python
if not await service.can_stream():
    await service.close()
    raise HTTPException(status_code=404, detail="Chat job not found")
```

A viewer reads only the final durable transcript, never transient owner frames.

- [ ] **Step 6: Prove GREEN**

Run:

```powershell
uv run pytest tests/service/chat/test_chat_message_sse_service.py tests/service/voice_realtime/test_turn_persistence_service.py tests/api/routers/chat/test_chat_messages_handler.py tests/api/routers/chat/test_voice_realtime_handler.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/service/chat/chat_message_sse_service.py src/api/routers/chat/messages_handler.py tests/service/chat/test_chat_message_sse_service.py tests/api/routers/chat/test_chat_messages_handler.py tests/service/voice_realtime/test_turn_persistence_service.py
git commit -m "fix: enforce owner-only chat job streaming"
```

---

### Task 8: Run The Feature Gate And Finalize Documentation

**Files:**
- Modify after verification: `docs/Technical Documentation/chat_bots/chat-session-sharing-architecture.md`
- Verify: all feature files above

- [ ] **Step 1: Run the full focused suite**

```powershell
uv run pytest tests/migrations/org/test_079_chat_session_access.py tests/migrations/test_org_migration_head_contract.py tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_service.py tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/service/chat/test_chat_message_sse_service.py tests/service/voice_realtime/test_turn_persistence_service.py tests/api/routers/chat/test_chat_sessions_handler.py tests/api/routers/chat/test_chat_sharing_handler.py tests/api/routers/chat/test_chat_messages_handler.py tests/api/routers/chat/test_voice_realtime_handler.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run static checks**

```powershell
uv run ruff format --check src/domain/models/organisation_tables/chat_session_access_models.py src/service/chat/chat_access.py src/service/chat/chat_session_service.py src/service/chat/chat_message_service.py src/service/chat/chat_message_sse_service.py src/api/schemas/chat_models.py src/api/routers/chat/sessions_handler.py src/api/routers/chat/messages_handler.py tests/migrations/org/test_079_chat_session_access.py tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/service/chat/test_chat_message_sse_service.py tests/api/routers/chat/test_chat_sharing_handler.py tests/api/routers/chat/test_chat_messages_handler.py
uv run ruff check src/domain/models/organisation_tables/chat_session_access_models.py src/service/chat/chat_access.py src/service/chat/chat_session_service.py src/service/chat/chat_message_service.py src/service/chat/chat_message_sse_service.py src/api/schemas/chat_models.py src/api/routers/chat/sessions_handler.py src/api/routers/chat/messages_handler.py tests/migrations/org/test_079_chat_session_access.py tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/service/chat/test_chat_message_sse_service.py tests/api/routers/chat/test_chat_sharing_handler.py tests/api/routers/chat/test_chat_messages_handler.py
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 3: Audit every invariant**

Verify with current code and test evidence:

- unique `(session_id, viewer_user_id)` grants;
- active, approved, same-org target validation;
- owner-only sharing and all mutations;
- explicit viewer reads the original committed messages;
- owner commits appear on viewer refresh without copies;
- attachments require an authorised session/message link;
- revocation blocks new reads and URLs;
- viewers cannot consume job SSE or persist voice turns; and
- no cloned, public, editor, group, or viewer-push feature was introduced.

- [ ] **Step 4: Mark architecture implemented**

Only after Steps 1–3 pass, change `**Status:** Proposed` to `**Status:** Implemented` and append the exact test/Ruff commands with observed pass counts.

- [ ] **Step 5: Commit documentation**

```powershell
git add 'docs/Technical Documentation/chat_bots/chat-session-sharing-architecture.md'
git commit -m "docs: mark chat sharing architecture implemented"
```

Do not deploy, push, or open a pull request without separate user authorization.
