# Chat Worker

This worker handles chat-related background tasks, specifically attachment processing.

## Tasks

### attachments.post_upload

- **Queue**: `attachments`
- **Purpose**: Process uploaded chat attachments (checksum calculation, thumbnail generation)
- **Args**: `organisation_id: str, attachment_id: str`

### Test Tasks

- **chat.ping**: Simple connectivity test
- **chat.test_task**: Test task with message parameter

## Queues

- **attachments**: For attachment processing tasks
- **chat_processing**: For general chat processing tasks
- **chat_messages**: For user chat message processing jobs (task `chat.process_message`)

## Usage

### Start the worker

```powershell
celery -A src.workers.chat_worker.celery_app worker -l info -Q attachments,chat_processing,chat_messages --pool=solo
```

### Test connectivity

```powershell
python test_chat_sender.py
```

## Configuration

- **Exchange**: `chat` (direct)
- **Default Queue**: `attachments`
- **Environment**: Uses same .env file as other workers

## Migration from Knowledge Worker

The `attachments.post_upload` task was moved from the knowledge worker to this dedicated chat worker to better separate concerns:

- Knowledge worker: Document processing, knowledge base operations
- Chat worker: Chat attachments, chat-related processing

The API routes in `src/api/routers/chat_attachments/upload_handler.py` have been updated to use the chat worker instead of the knowledge worker.
