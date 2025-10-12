Chat Sessions, Messages & SSE (Technical)

Scope: src/api/routers/chats, src/service/chat, src/api/schemas/chat_models.py, src/api/schemas/chat_stream_models.py

Overview
- Session management: create, rename, favorite, delete (soft), list, search.
- Messaging: persist user messages (with optional attachments) and enqueue background jobs for assistant responses.
- Streaming: Server-Sent Events (SSE) frames provide real-time answer updates, suggestions, sources, progress.
- Tenancy: all reads/writes scoped to the authenticated user and organisation.

API Surface (mounted under `/users/chats`)
- POST `/create_chat`, DELETE `/delete_chat`, PATCH `/rename_chat`, PATCH `/favorite_chat`
- GET `/list_chats`, GET `/search_chats?q=...`
- POST `/send_message`
- GET `/message_events?job_id=...` (SSE)
- GET `/messages/list?session_id=...&limit=...&cursor=...` (cursor pagination)

Response Envelope
- All endpoints respond with `GenericResponse[T]` (see `src/api/schemas/base_models.py`).

Pydantic Schemas (Requests/Responses)
- Source of truth: `src/api/schemas/chat_models.py`
- Optional fields are declared as `Optional[...]` or have defaults; all others are required.

```python
from typing import Optional, List, Dict, Any
from uuid import UUID
from pydantic import BaseModel, Field

class ChatSessionCreate(BaseModel):
    title: str
    tags: Optional[List[str]] = None

class ChatSessionOut(BaseModel):
    session_id: UUID
    title: str
    chat_type: str
    is_favorite: bool = False
    tags: List[str] = []
    created_at: str  # ISO8601
    updated_at: str  # ISO8601

class DeleteChat(BaseModel):
    session_id: UUID

class RenameChat(BaseModel):
    session_id: UUID
    title: str

class FavoriteChat(BaseModel):
    session_id: UUID
    favorite: bool

class ChatSessionsPage(BaseModel):
    items: List[ChatSessionOut]
    cursor: Optional[str] = None

class ChatAttachmentView(BaseModel):
    attachment_id: UUID
    filename: str
    mimetype: str
    thumbnail_small_url: Optional[str] = None

class SendMessageRequest(BaseModel):
    session_id: UUID
    content: str
    attachments: Optional[List[UUID]] = None
    message_type: Optional[str] = "GENERAL_MESSAGE"  # ChatMessageType
    role: str = "user"                              # ChatMessageRole
    is_edited: bool = False
    edited_message_id: Optional[UUID] = None

class SendMessageResponse(BaseModel):
    message_id: UUID
    job_id: UUID

class ChatMessageListItem(BaseModel):
    message_id: UUID
    session_id: UUID
    role: str                # ChatMessageRole
    message_type: str        # ChatMessageType
    content: str
    is_edited: bool = False
    created_at: str          # ISO8601
    updated_at: str          # ISO8601
    attachments: List[ChatAttachmentView] = []
    confidence_score: Optional[str] = None
    sources: Optional[List[Dict[str, Any]]] = None
    suggestions: Optional[List[str]] = None

class ChatMessagesPage(BaseModel):
    items: List[ChatMessageListItem]
    cursor: Optional[str] = None
```

Validation Notes
- `SendMessageRequest.content` is trimmed and must be non-empty.
- `SendMessageRequest.role` must be `user`.
- If `is_edited` is true, `edited_message_id` is required.

SSE Frames (Streaming)
- Source of truth: `src/api/schemas/chat_stream_models.py`
- Representative frames (frontend should switch on `type`):

```python
from typing import Literal, Optional, List, Union, Dict, Any
from pydantic import BaseModel, Field

class AnswerDelta(BaseModel):
    type: Literal["answer.delta"] = "answer.delta"
    content: str
    format: Optional[Literal["markdown", "plain"]] = None

class AnswerReplace(BaseModel):
    type: Literal["answer.replace"] = "answer.replace"
    content: str
    format: Optional[Literal["markdown", "plain"]] = None

class AnswerMetrics(BaseModel):
    type: Literal["answer.metrics"] = "answer.metrics"
    confidence: Optional[Literal["high", "medium", "low"]] = None

class SuggestionsReplace(BaseModel):
    type: Literal["suggestions.replace"] = "suggestions.replace"
    items: List[Union[str, Dict[str, Any]]]

class SourcesReplace(BaseModel):
    type: Literal["sources.replace"] = "sources.replace"
    sources: Optional[List[Dict[str, Any]]] = None

class ProgressUpdate(BaseModel):
    type: Literal["progress.update"] = "progress.update"
    message: str
    stage: Optional[str] = None
    progress: Optional[float] = None  # 0..1

class Finalize(BaseModel):
    type: Literal["finalize"] = "finalize"
    reason: Optional[Literal["done", "aborted", "error"]] = None
```

Frontend Handling
- Use `answer.delta` to append and `answer.replace` to overwrite.
- Update confidence, suggestions, sources, and progress on corresponding frames.
- Stop on SSE `complete` event; reconnect with backoff on `timeout`/transient errors.

Tenancy & Security
- All operations scoped to authenticated `user_id` and `organisation_id`.
- Soft-deleted sessions are filtered from list/search by default.

