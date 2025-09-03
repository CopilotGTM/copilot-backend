from __future__ import annotations
import time
import threading
from typing import Optional, Dict


class TTLSecretCache:
    """Minimal thread-safe TTL cache for secrets.

    Requirements for a secret cache are modest:
    - VERY low overhead (hot path before every network call).
    - Expire entries after a fixed TTL (rotation eventually becomes visible).
    - Bounded memory to avoid unbounded growth when many organisations exist.

    This implementation intentionally keeps policy simple (FIFO eviction when
    full). If precise recency ordering is needed later we can swap to an LRU.

    Parameters
    ----------
    ttl_seconds : int
        Base time-to-live for each entry.
    max_size : int
        Upper bound on stored secrets. When exceeded, oldest key is evicted.
    jitter : float
        (Reserved) Potential future use to add TTL jitter to avoid thundering
        herd refresh; currently unused but kept for API stability.
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 1000, jitter: float = 0.1):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self.jitter = jitter
        self._data: Dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> Optional[str]:
        """Return cached secret value if present and not expired."""
        with self._lock:
            item = self._data.get(name)
            if not item:
                return None
            value, ts = item
            if time.time() - ts > self.ttl:
                # Expired
                self._data.pop(name, None)
                return None
            return value

    def set(self, name: str, value: str):
        """Store / update secret value applying max size eviction."""
        with self._lock:
            if len(self._data) >= self.max_size:
                # Simple FIFO eviction (predictable & cheap)
                oldest = next(iter(self._data.keys()))
                self._data.pop(oldest, None)
            self._data[name] = (value, time.time())

    def invalidate(self, name: str):
        """Remove a single entry (e.g. after rotation)."""
        with self._lock:
            self._data.pop(name, None)

    def clear(self):
        """Clear all cached secrets (e.g. operational flush)."""
        with self._lock:
            self._data.clear()
