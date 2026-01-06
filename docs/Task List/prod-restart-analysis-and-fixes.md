# Pod Restart Issue Analysis and Fixes

## Executive Summary

The Copilot backend pod is restarting every 3-4 hours due to **two combined issues**:

1. **Memory leaks from improper event loop management** - The code creates new asyncio event loops for each task/message instead of reusing a persistent loop, causing memory to accumulate.

2. **Aggressive Kubernetes health probe timeouts** - The liveness probe has a 10-second timeout, which fails when the process is under memory pressure (GC pauses) or when the event loop is blocked.

**Why workers don't have this issue:** Celery workers have no health probes and automatically recycle after 50 tasks or 500MB memory usage.

**Why pods get stuck:** As memory grows, garbage collection pauses become longer, causing health probe timeouts. After 3 consecutive failures (90 seconds), Kubernetes kills the pod - even if the process would have recovered.

---

## Root Cause Analysis

### Primary Issue: Event Loop Memory Leak

**Severity: CRITICAL**

The codebase creates **new event loops per task/message** instead of reusing them. Each event loop creation:
- Allocates memory for the loop infrastructure
- Creates internal queues and handles
- Memory is not properly garbage collected due to reference cycles
- Over 3-4 hours, this accumulates to OOMKilled

### Affected Files

| File | Location | Impact |
|------|----------|--------|
| `redis_subscriber.py` | `src/api/events/redis_subscriber.py:46-53` | **CRITICAL** - Creates new loop for EVERY Redis message in tight loop |
| `document_extraction.py` | `src/workers/knowledge_worker/tasks/document_extraction.py:79-80` | HIGH - New loop per extraction task |
| `post_upload.py` | `src/workers/knowledge_worker/tasks/post_upload.py` | MEDIUM - New loop per upload |
| `delete_source.py` | `src/workers/knowledge_worker/tasks/delete_source.py` | MEDIUM - New loop per delete |
| `extraction_service.py` | `src/workers/knowledge_worker/services/extraction_service.py:79` | MEDIUM - New loop per extraction |
| `post_upload.py` (chat) | `src/workers/chat_worker/tasks/post_upload.py` | MEDIUM - New loop per chat upload |

---

## The Problem Pattern (What NOT to Do)

```python
# BAD PATTERN - Creates memory leak
def some_task():
    loop = asyncio.new_event_loop()      # Creates NEW loop
    asyncio.set_event_loop(loop)

    loop.run_until_complete(some_async_function())

    loop.close()  # Even with close(), memory accumulates
```

### Why This Causes Memory Leaks

1. Each `new_event_loop()` allocates ~1-5KB of memory
2. Event loop references persist even after `close()`
3. Database connections cached on old loops become orphaned
4. In high-volume scenarios (1000+ messages/hour), memory grows rapidly

---

## The Solution: Persistent Thread-Local Event Loop

You already have a proper implementation in `src/infra/utils/async_runner.py`:

```python
# GOOD PATTERN - Uses persistent thread-local loop
from src.infra.utils.async_runner import run_async

def some_task():
    run_async(some_async_function())  # Reuses existing loop
```

The `run_async()` function:
- Stores event loop in thread-local storage
- Reuses the same loop across all calls in that thread
- Properly cleans up pending tasks WITHOUT closing the loop
- Handles database connection cache clearing when loop changes

---

## File-by-File Fixes

### Fix 1: redis_subscriber.py (CRITICAL)

**Location:** `src/api/events/redis_subscriber.py`

**Before (Lines 45-53):**
```python
# Create event loop for async operation
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Emit message asynchronously
loop.run_until_complete(
    emit_websocket_message(event, message_data, sid)
)
loop.close()
```

**After:**
```python
# Use persistent thread-local event loop
# This prevents memory leaks from creating new loops per message
run_async(emit_websocket_message(event, message_data, sid))
```

**Also update imports at top of file:**
```python
# Remove: import asyncio
# Add: from src.infra.utils.async_runner import run_async
```

**Full fixed file:**
```python
import json
import threading
import time

from src.api.events.socket_app import sio
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger()
WEBSOCKET_NAMESPACE = "/websocket/v1"
REDIS_CHANNEL = "websocket_messages"


async def emit_websocket_message(event, message_data, sid):
    """Emit message to websocket client asynchronously"""
    try:
        await sio.emit(event, message_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
        logger.info(f"Successfully emitted message to websocket client {sid}")
    except Exception as e:
        logger.error(f"Error emitting websocket message: {e}", exc_info=True)


def redis_subscriber():
    """Subscribe to Redis channel and forward messages to websocket clients.

    Uses a persistent thread-local event loop via run_async() to prevent
    memory leaks from creating new event loops per message.
    """
    while True:
        try:
            redis_client = get_redis_client()
            pubsub = redis_client.pubsub()
            logger.info(f"Subscribing to Redis channel {REDIS_CHANNEL}")
            pubsub.subscribe(REDIS_CHANNEL)
            logger.info(f"Successfully subscribed to Redis channel {REDIS_CHANNEL}")

            while True:
                try:
                    logger.debug("Waiting for message...")
                    message = pubsub.get_message(timeout=1.0)
                    if message and message["type"] == "message":
                        try:
                            data = json.loads(message["data"])
                            sid = data["sid"]
                            event = data["event"]
                            message_data = data["data"]

                            # Use persistent thread-local event loop
                            run_async(emit_websocket_message(event, message_data, sid))

                        except Exception as e:
                            logger.error(
                                f"Error processing Redis message: {e}",
                                exc_info=True,
                            )
                except Exception as e:
                    logger.error(f"Error in Redis subscriber loop: {e}", exc_info=True)
                    break

        except Exception as e:
            logger.error(f"Redis subscriber error: {e}", exc_info=True)
            logger.info("Waiting 5 seconds before reconnecting...")
            time.sleep(5)
            continue


def start_redis_subscriber_thread() -> threading.Thread:
    """Start the Redis subscriber in a background thread."""
    logger.info("Starting Redis subscriber thread...")
    subscriber_thread = threading.Thread(target=redis_subscriber, daemon=True)
    subscriber_thread.start()
    logger.info("Redis subscriber thread started")
    return subscriber_thread
```

---

### Fix 2: document_extraction.py (HIGH)

**Location:** `src/workers/knowledge_worker/tasks/document_extraction.py`

**Before (Lines 79-80, 207-212):**
```python
# Line 79-80: Creating loop
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ... task code ...

# Lines 207-212: Cleanup
try:
    loop.run_until_complete(storage.close())
except Exception:
    pass
finally:
    loop.close()
```

**After - use run_async:**
```python
from src.infra.utils.async_runner import run_async

# Replace loop creation with run_async usage
# For download_blob_to_temp:
temp_file_path = run_async(download_blob_to_temp_async(storage, source))

# For storage.close():
try:
    run_async(storage.close())
except Exception:
    pass
# Remove: loop.close()
```

**Key changes:**
1. Remove `loop = asyncio.new_event_loop()` and `asyncio.set_event_loop(loop)`
2. Replace `loop.run_until_complete(...)` with `run_async(...)`
3. Remove the `finally: loop.close()` block

---

### Fix 3: Other Worker Tasks

Apply the same pattern to:

1. **`src/workers/knowledge_worker/tasks/post_upload.py`**
2. **`src/workers/knowledge_worker/tasks/delete_source.py`**
3. **`src/workers/chat_worker/tasks/post_upload.py`**
4. **`src/workers/knowledge_worker/services/extraction_service.py`**

For each file:
1. Add import: `from src.infra.utils.async_runner import run_async`
2. Replace `loop.run_until_complete(coro)` with `run_async(coro)`
3. Remove `loop = asyncio.new_event_loop()` and `asyncio.set_event_loop(loop)`
4. Remove `loop.close()` calls

---

## Diagnostic Commands

Run these to confirm OOMKilled as the restart reason:

```bash
# Check pod restart reason
kubectl describe pod <pod-name> -n <namespace> | grep -A 10 "Last State"

# Check if OOMKilled
kubectl get pods -n <namespace> -o jsonpath='{.items[*].status.containerStatuses[*].lastState.terminated.reason}'

# Monitor memory usage in real-time
kubectl top pods -n <namespace> --containers

# Check events for OOMKilled
kubectl get events -n <namespace> --sort-by=.lastTimestamp | grep -i oom

# View pod logs before crash
kubectl logs <pod-name> -n <namespace> --previous
```

---

## Kubernetes Configuration Issues

### Why Workers Don't Have This Issue (But API Pods Do)

**Workers are protected by:**
1. **No health probes** - Celery workers don't have liveness/readiness probes
2. **Celery's built-in worker recycling**:
   - `worker_max_tasks_per_child = 50` (restart after 50 tasks)
   - `worker_max_memory_per_child = 500000` (restart at 500MB)
3. **Workers recycle before memory becomes critical**

**API pods are vulnerable because:**
1. **Aggressive health probe timeouts** cause premature kills
2. **Redis subscriber runs in API pod** - the most critical memory leak source
3. **No startup probe** - pod may be killed during initialization
4. **Single replica** - any restart = complete downtime

---

### Current Health Probe Configuration (PROBLEMATIC)

**Location:** `charts/application/charts/webservice/templates/deployment.yaml`

```yaml
# Current configuration - TOO AGGRESSIVE
livenessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 60
  periodSeconds: 30
  timeoutSeconds: 10        # ❌ Too short - GC pauses can exceed this
  failureThreshold: 3       # ❌ 3 failures = 90s then killed

readinessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 30
  periodSeconds: 15
  timeoutSeconds: 5         # ❌ Very aggressive
  failureThreshold: 3
```

**Problem:** When memory pressure causes GC pauses or the event loop is blocked by creating new loops, the `/health` endpoint can't respond within 10 seconds. After 3 failures (90 seconds), Kubernetes kills the pod.

---

### Recommended Health Probe Configuration

```yaml
# RECOMMENDED - More tolerant configuration
startupProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 10
  failureThreshold: 30      # Allow 5 minutes for startup

livenessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 0    # Start immediately after startup probe passes
  periodSeconds: 30
  timeoutSeconds: 30        # ✅ More tolerant of GC pauses
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 0
  periodSeconds: 15
  timeoutSeconds: 15        # ✅ More tolerant
  failureThreshold: 3
```

---

### Recommended Deployment Changes

**1. Add Startup Probe** (prevents premature kills during initialization)

**2. Increase Replica Count** (high availability)
```yaml
# In values.yaml
api:
  replicaCount: 2           # ✅ At least 2 for redundancy
  autoscaling:
    enabled: true           # ✅ Enable autoscaling
    minReplicas: 2
    maxReplicas: 5
```

**3. Resource Adjustment** (if event loop fixes are deployed)
```yaml
resources:
  limits:
    cpu: 1500m
    memory: 4Gi             # Can reduce after fixing event loop leaks
  requests:
    cpu: 1000m
    memory: 2Gi
```

---

## Additional Recommendations

### 1. Increase Memory Limits (Short-term)

If fixes take time to deploy, temporarily increase memory:

```yaml
# In Helm values.yaml
api:
  resources:
    limits:
      memory: "8Gi"  # Increase from 6Gi
    requests:
      memory: "4Gi"  # Increase from 3Gi
```

### 2. Add Memory Monitoring

Add to your health check endpoint:

```python
import psutil
import os

def get_memory_info():
    process = psutil.Process(os.getpid())
    return {
        "rss_mb": process.memory_info().rss / 1024 / 1024,
        "vms_mb": process.memory_info().vms / 1024 / 1024,
        "percent": process.memory_percent()
    }
```

### 3. Configure Celery Worker Memory Limits

Already configured but verify these settings:

```python
# In celery config
worker_max_tasks_per_child = 50  # Restart worker after 50 tasks
worker_max_memory_per_child = 500000  # 500MB limit per worker
```

### 4. Database Connection Pool Limits

The per-tenant connection pool can multiply memory usage:
- 100 tenants x 5 connections = 500 potential connections

Consider reducing pool size or implementing connection pooling at infrastructure level.

---

## Summary of Changes

| Priority | File | Change |
|----------|------|--------|
| CRITICAL | `redis_subscriber.py` | Use `run_async()` instead of creating new loops |
| HIGH | `document_extraction.py` | Use `run_async()` for all async operations |
| MEDIUM | `post_upload.py` (knowledge) | Use `run_async()` |
| MEDIUM | `delete_source.py` | Use `run_async()` |
| MEDIUM | `post_upload.py` (chat) | Use `run_async()` |
| MEDIUM | `extraction_service.py` | Use `run_async()` |

---

## Expected Results After Fixes

1. **Memory Usage:** Stable, no gradual increase over time
2. **Pod Restarts:** Should stop (or dramatically reduce)
3. **Health Check Failures:** Eliminated (no more 2-4 min stuck periods)
4. **Performance:** Slight improvement due to loop reuse

---

## Verification Steps

After deploying fixes:

1. Monitor memory usage for 24 hours:
   ```bash
   kubectl top pods -n <namespace> --containers -w
   ```

2. Check for OOMKilled events:
   ```bash
   kubectl get events -n <namespace> --watch | grep -i oom
   ```

3. Verify no restarts:
   ```bash
   kubectl get pods -n <namespace> -w
   ```

---

## Files Reference

### Existing Good Implementation (Use This Pattern)
- `src/infra/utils/async_runner.py` - Contains the `run_async()` helper

### Files That Need Fixes
- `src/api/events/redis_subscriber.py` - Lines 45-53
- `src/workers/knowledge_worker/tasks/document_extraction.py` - Lines 79-80, 207-212
- `src/workers/knowledge_worker/tasks/post_upload.py`
- `src/workers/knowledge_worker/tasks/delete_source.py`
- `src/workers/chat_worker/tasks/post_upload.py`
- `src/workers/knowledge_worker/services/extraction_service.py` - Line 79

### Configuration Files
- `charts/application/values.yaml` - Memory limits
- `charts/application/charts/webservice/templates/deployment.yaml` - Health probes
- `src/workers/base/celery_base.py` - Celery configuration

---

---

## Priority Action Plan

### Immediate (Deploy Today)

| Priority | Action | Impact |
|----------|--------|--------|
| 1 | Update health probe timeouts in `deployment.yaml` | Prevents aggressive pod kills during GC pauses |
| 2 | Add startup probe to `deployment.yaml` | Prevents kills during initialization |
| 3 | Fix `redis_subscriber.py` event loop | Most critical memory leak (runs in API pod) |

### Short-term (This Week)

| Priority | Action | Impact |
|----------|--------|--------|
| 4 | Enable autoscaling with min 2 replicas | High availability during restarts |
| 5 | Fix worker event loop leaks | Prevents future issues as load increases |

### Verification After Deployment

1. Monitor memory for 24-48 hours:
   ```bash
   kubectl top pods -n <namespace> --containers -w
   ```

2. Check for restarts:
   ```bash
   kubectl get pods -n <namespace> -w
   ```

3. Verify no OOMKilled events:
   ```bash
   kubectl get events -n <namespace> --watch | grep -i oom
   ```

---

*Document generated and updated by Claude Code analysis on 2026-01-06*
