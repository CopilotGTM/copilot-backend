# Chat Attachment Flow Testing Guide

Scope: web chat, Slack bot chat, Teams bot chat, and agent-side attachment reading.

## What to Read First

Read these files in this order if you want to understand the full flow quickly.

1. `src/service/chat/external_chat_attachment_ingest_service.py`
   This is the normalization layer for Slack and Teams attachments. It downloads provider files, uploads them into our own blob storage, creates `Attachment` rows, and stores derived transcript text for audio when available.

2. `src/service/bots/slack_bot_service.py`
   See how Slack messages ingest attachments before the bot job is queued, and how personal-chat messages link ingested attachments to the persisted user message.

3. `src/service/bots/teams_bot_service.py`
   Same idea as Slack, but for Teams attachment shapes and token-backed downloads.

4. `src/workers/chat_worker/services/message_processing_service.py`
   This is the web chat path. It loads chat history, rebuilds per-message attachment manifests, and only sends current-turn images as native vision input.

5. `src/workers/chat_worker/services/bot_message_processing_service.py`
   This is the bot worker path. It rebuilds attachment manifests for persisted personal chats and exposes current-turn attachment IDs to the agent.

6. `src/agents/tools/communication/chat_attachments.py`
   This is the main runtime attachment tool. The agent reads attachments by internal `attachment_id` using `read_chat_attachment`.

7. `src/agents/utils.py`
   Confirms how each message gets its own attachment manifest appended during message preparation.

8. `src/agents/workflows/general/prompts/general_prompt.py`
   Confirms the intended agent behavior: read manifests first, then call `read_chat_attachment` when needed.

9. `src/domain/models/organisation_tables/chat_message_models.py`
   Check the persisted attachment metadata fields: `source_provider`, `source_external_id`, `normalized_text_path`, and `normalized_text_status`.

10. `migrations/org_migrations/versions/3c7f79637798_030_add_attachment_ingest_metadata.py`
    Confirms the schema change behind the new metadata.

## Flow Summary

### Web Chat

1. User uploads file from the web UI.
2. File is stored in our blob storage through the existing chat upload flow.
3. User sends a message with attachment IDs.
4. `message_processing_service.py` loads recent messages and rebuilds an attachment manifest for each message.
5. The agent sees the manifest in message history.
6. If needed, the agent calls `read_chat_attachment(attachment_id=...)`.

### Slack Personal Chat

1. User sends a Slack DM with a file.
2. `slack_bot_service.py` downloads the Slack file immediately.
3. The file is uploaded into our own blob storage and saved in `attachments`.
4. If the Slack conversation is personal, the file is linked to the persisted `ChatMessage`.
5. The worker reloads chat history from DB and rebuilds attachment manifests.
6. The agent reads the attachment by internal ID, not by Slack URL.

### Teams Personal Chat

1. User sends a Teams personal message with a file.
2. `teams_bot_service.py` downloads the Teams file immediately.
3. The file is uploaded into our own blob storage and saved in `attachments`.
4. If the Teams conversation is personal, the file is linked to the persisted `ChatMessage`.
5. The worker reloads chat history from DB and rebuilds attachment manifests.
6. The agent reads the attachment by internal ID, not by Teams `downloadUrl`.

## UI Test Flows

### Flow 1: Web Chat, Same-Turn File Question

Goal: verify current web attachments appear in history and can be read by the agent.

1. Open web chat.
2. Upload a PDF or TXT file.
3. Ask a direct question about the file, for example: `Summarize this file in 5 bullets.`
4. Confirm the answer clearly uses the file contents.
5. Open DB or logs if needed and verify:
   - an `attachments` row exists
   - `source_provider = web`
   - the user message is linked in `chat_message_attachment_link`

### Flow 2: Web Chat, Follow-Up on Previous File

Goal: verify older file references still work without re-upload.

1. In web chat, upload a file and ask an initial question.
2. After the assistant answers, ask a follow-up like: `What did page 2 say again?`
3. Confirm the assistant can answer from the same file.
4. This proves the manifest was rebuilt from persisted chat history and the agent could re-read the file by internal ID.

### Flow 3: Slack Personal Chat Attachment

Goal: verify Slack files are normalized into internal storage before processing.

1. Send the bot a Slack DM with a file and a question.
2. Confirm the bot answers from the file.
3. Ask a follow-up in the same Slack DM: `Answer again from that file.`
4. Confirm it still works.
5. Verify in DB/storage if needed:
   - attachment row exists with `source_provider = slack`
   - file is linked to the persisted personal chat message
   - no later dependency on `url_private_download` is needed for the follow-up turn

### Flow 4: Teams Personal Chat Attachment

Goal: verify Teams files are normalized immediately and no follow-up depends on provider URLs.

1. Send the bot a Teams personal message with a file and a question.
2. Confirm the bot answers from the file.
3. Ask a follow-up like: `Can you quote that file again?`
4. Confirm it still works in the same personal chat.
5. Verify in DB/storage if needed:
   - attachment row exists with `source_provider = teams`
   - file is linked to the persisted personal chat message

### Flow 5: Audio Attachment

Goal: verify transcripts are stored as normalized text and readable later.

1. Upload or send an audio file.
2. Ask for a summary.
3. Confirm the answer is based on the transcript.
4. Verify in DB if needed:
   - `normalized_text_status = ready` when transcription succeeded
   - `normalized_text_path` is populated
5. Ask a follow-up question on the same audio file.
6. Confirm the assistant can answer again without needing the original provider URL.

### Flow 6: Image Attachment

Goal: verify images stay native for the current turn.

1. Upload an image and ask a visual question.
2. Confirm the assistant answers from the image.
3. For a later follow-up, confirm the assistant can still identify the attachment in history, but current-turn image understanding is strongest when the image is attached on that turn.

## What Good Looks Like

A flow is working correctly when all of these are true:

- The message history shows attachment manifests on the correct user turns.
- Follow-up questions about old files work without re-uploading the file.
- Slack and Teams follow-up turns do not depend on provider URLs still being valid.
- The agent reads attachments through `read_chat_attachment` using internal attachment IDs.
- Current-turn images work natively.
- Audio files can expose a stored normalized transcript.

## Fast Debug Checklist

If a flow fails, check these in order:

1. Was the file ingested into our own storage?
2. Was an `attachments` row created with the right `source_provider`?
3. For personal chat, was `chat_message_attachment_link` created?
4. Does the rebuilt message history include an attachment manifest for that user message?
5. Does the agent context include the right `chat_session_id`, `current_user_message_id`, or `allowed_attachment_ids`?
6. Can `read_chat_attachment` load the attachment directly from internal storage?
7. For audio, is `normalized_text_status` ready?

## Recommended Manual Test Order

If you want the quickest confidence pass, do the tests in this order:

1. Web same-turn file question
2. Web follow-up on previous file
3. Slack personal chat file + follow-up
4. Teams personal chat file + follow-up
5. Audio attachment
6. Image attachment
