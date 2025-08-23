# Knowledge Source SSE Implementation

## Overview

This implementation provides a production-ready Server-Sent Events (SSE) streaming solution for knowledge source status updates using Redis pub/sub for real-time notifications.

## Architecture

```
[Upload API] -> [Celery Tasks] -> [Redis Pub/Sub] -> [SSE Stream] -> [Frontend]
     |              |                    |              |
     |              |                    |              |
   Finalize     Status Updates      Channel:        EventSource
   Source      (Processing,         kb:{org}:       /sources/stream
              Extracting,           sources
              Completed, Failed)
```

## Key Components

### 1. Redis Pub/Sub Infrastructure

**File:** `src/infra/messaging/redis_pubsub.py`
- Async Redis client with proper connection management
- Channel subscription with message parsing
- Error handling and connection cleanup

**File:** `src/infra/messaging/knowledge_publish.py`
- Sync Redis publishing for Celery tasks
- Consistent channel naming: `kb:{organisation_id}:sources`
- Lightweight message payloads

### 2. SSE Service Layer

**File:** `src/service/knowledge_base/knowledge_sse_service.py`
- Production-ready SSE streaming with Redis pub/sub
- Initial snapshot + real-time updates
- Heartbeat pings to prevent proxy timeouts
- Proper timeout handling with reconnection guidance

### 3. API Endpoint

**Updated:** `src/api/routers/knowledge_base/upload_handler.py`
- Clean endpoint using service abstraction
- 30-minute default timeout (configurable)
- Comprehensive documentation for frontend integration

## Message Flow

### 1. Initial Connection
```javascript
const eventSource = new EventSource('/api/users/knowledge/sources/stream?source_ids=uuid1,uuid2');
```

### 2. Event Types

**snapshot** - Initial state of all monitored sources
```json
{
  "event": "snapshot",
  "data": "[{\"source_id\": \"...\", \"status\": \"processing\", ...}]"
}
```

**update** - Real-time status change (single source)
```json
{
  "event": "update", 
  "data": "[{\"source_id\": \"...\", \"status\": \"completed\", \"updated_at\": \"...\"}]"
}
```

**complete** - All sources terminal, stream closing
```json
{
  "event": "complete",
  "data": "{\"message\": \"all_sources_completed\", \"final_statuses\": {...}}"
}
```

**timeout** - Stream timeout, client should reconnect
```json
{
  "event": "timeout",
  "data": "{\"message\": \"Stream timeout reached. Please reconnect.\", \"reconnect\": true}"
}
```

**ping** - Heartbeat (every 25s)
```json
{
  "event": "ping",
  "data": "{\"timestamp\": 1724339200}"
}
```

**error** - Unrecoverable error
```json
{
  "event": "error",
  "data": "{\"message\": \"Internal stream error. Please reconnect.\", \"reconnect\": true}"
}
```

## Publishing Points

Status updates are published from:

1. **API finalize_source** → PROCESSING transition
2. **post_upload_process task** → Job progress updates  
3. **document_extraction task** → EXTRACTING, COMPLETED, FAILED transitions

## Frontend Integration

```javascript
const eventSource = new EventSource('/api/users/knowledge/sources/stream');

eventSource.addEventListener('snapshot', (event) => {
  const sources = JSON.parse(event.data);
  // Initialize UI with current state
});

eventSource.addEventListener('update', (event) => {
  const updates = JSON.parse(event.data);
  // Update specific sources in UI
});

eventSource.addEventListener('timeout', (event) => {
  // Reconnect logic
  eventSource.close();
  setTimeout(() => {
    // Recreate connection
  }, 1000);
});

eventSource.addEventListener('complete', (event) => {
  // All uploads finished
  eventSource.close();
});
```

## Production Considerations

### Timeouts & Reconnection
- **Default timeout:** 30 minutes (sufficient for large batch processing)
- **Heartbeat:** 25-second intervals prevent proxy idle timeouts
- **Reconnection:** Clear guidance in timeout/error events

### Scalability
- **Per-org channels:** Isolated Redis pub/sub channels prevent cross-org leakage
- **Filtering:** Client-side filtering by source IDs reduces bandwidth
- **Lightweight messages:** Only essential fields published, full data in snapshot

### Error Handling
- **Redis failures:** Graceful degradation with logging
- **Connection issues:** Automatic cleanup and error events
- **Invalid input:** Clear error responses for malformed requests

### Dependencies
- **Redis:** Upgraded to `>=5.0.0` for async support
- **sse-starlette:** For EventSourceResponse (already in requirements)

## Testing

Run the test script to verify functionality:

```bash
cd /path/to/copilot-backend
python test_knowledge_sse.py
```

This will test:
- Redis connectivity
- Message publishing/receiving  
- Upload workflow simulation
- Channel isolation

## Deployment Notes

1. **Redis Requirements:**
   - Ensure Redis version supports pub/sub
   - Configure appropriate `maxclients` for concurrent SSE connections
   - Monitor Redis memory usage with many active subscriptions

2. **Proxy Configuration:**
   - Ensure reverse proxies don't buffer SSE responses
   - Set appropriate timeout values (longer than heartbeat interval)
   - Configure keepalive settings

3. **Monitoring:**
   - Monitor active SSE connections
   - Track Redis pub/sub channel activity
   - Alert on unusual timeout rates

## Migration Path

For existing polling-based implementations:

1. **Phase 1:** Deploy new SSE endpoint alongside old polling
2. **Phase 2:** Update frontend to use SSE with polling fallback
3. **Phase 3:** Remove old polling endpoints after monitoring confirms stability
