from __future__ import annotations
import time
import threading
from typing import Optional, Dict, Tuple
from src.infra.utils.logger import get_logger

logger = get_logger("secret_cache")


class TTLSecretCache:
    """Enhanced thread-safe TTL cache for secrets with production reliability features.

    Requirements for a secret cache are modest:
    - VERY low overhead (hot path before every network call).
    - Expire entries after a fixed TTL (rotation eventually becomes visible).
    - Bounded memory to avoid unbounded growth when many organisations exist.
    - Grace period for expired entries during provider failures.
    - Metrics for monitoring cache effectiveness.

    This implementation keeps policy simple (FIFO eviction when full) while adding
    production-ready features like grace periods and metrics.

    Parameters
    ----------
    ttl_seconds : int
        Base time-to-live for each entry.
    max_size : int
        Upper bound on stored secrets. When exceeded, oldest key is evicted.
    grace_period : int
        Additional time to keep expired entries for fallback scenarios.
    jitter : float
        (Reserved) Potential future use to add TTL jitter to avoid thundering
        herd refresh; currently unused but kept for API stability.
    """

    def __init__(
        self, 
        ttl_seconds: int = 900,  # Increased default TTL 
        max_size: int = 1000, 
        grace_period: int = 3600,  # 1 hour grace period for expired entries
        jitter: float = 0.1
    ):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self.grace_period = grace_period
        self.jitter = jitter
        self._data: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()
        
        # Metrics
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expired_hits = 0  # Hits on expired but within grace period

    def get(self, name: str) -> Optional[str]:
        """Return cached secret value if present and not expired."""
        with self._lock:
            item = self._data.get(name)
            if not item:
                self._misses += 1
                return None
            
            value, ts = item
            age = time.time() - ts
            
            if age <= self.ttl:
                # Fresh cache hit
                self._hits += 1
                return value
            elif age <= self.ttl + self.grace_period:
                # Expired but within grace period - could be used as fallback
                # Don't count as regular hit, but don't remove either
                return None
            else:
                # Fully expired, remove
                self._data.pop(name, None)
                self._misses += 1
                return None

    def get_expired(self, name: str, max_age: int) -> Optional[str]:
        """Get an expired entry if it's within the specified max_age.
        
        This is used for fallback scenarios when the provider is unavailable
        but we need to keep systems running with stale but recent data.
        """
        with self._lock:
            item = self._data.get(name)
            if not item:
                return None
            
            value, ts = item
            age = time.time() - ts
            
            if age <= max_age:
                if age > self.ttl:
                    self._expired_hits += 1
                    logger.debug(f"Using expired cache entry for '{name}' (age: {age:.1f}s)")
                else:
                    self._hits += 1
                return value
            
            return None

    def set(self, name: str, value: str):
        """Store / update secret value applying max size eviction."""
        with self._lock:
            # Check if we need to evict before adding
            if name not in self._data and len(self._data) >= self.max_size:
                # Simple FIFO eviction (predictable & cheap)
                oldest_key = next(iter(self._data.keys()))
                self._data.pop(oldest_key, None)
                self._evictions += 1
                logger.debug(f"Evicted oldest cache entry: {oldest_key}")
            
            self._data[name] = (value, time.time())
            logger.debug(f"Cached secret '{name}' with TTL {self.ttl}s")

    def invalidate(self, name: str):
        """Remove a single entry (e.g. after rotation)."""
        with self._lock:
            removed = self._data.pop(name, None)
            if removed:
                logger.debug(f"Invalidated cache entry for '{name}'")

    def clear(self):
        """Clear all cached secrets (e.g. operational flush)."""
        with self._lock:
            count = len(self._data)
            self._data.clear()
            logger.info(f"Cleared {count} cache entries")

    def cleanup_expired(self):
        """Remove all fully expired entries (beyond grace period).
        
        This can be called periodically to prevent memory growth from
        expired entries that are never accessed again.
        """
        current_time = time.time()
        expired_keys = []
        
        with self._lock:
            for key, (value, ts) in self._data.items():
                if current_time - ts > self.ttl + self.grace_period:
                    expired_keys.append(key)
            
            for key in expired_keys:
                self._data.pop(key, None)
        
        if expired_keys:
            logger.info(f"Cleaned up {len(expired_keys)} expired cache entries")

    def get_stats(self) -> Dict[str, any]:
        """Get cache statistics for monitoring."""
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = self._hits / max(1, total_requests)
            
            # Count fresh vs expired entries
            current_time = time.time()
            fresh_count = 0
            expired_count = 0
            
            for value, ts in self._data.values():
                age = current_time - ts
                if age <= self.ttl:
                    fresh_count += 1
                else:
                    expired_count += 1
            
            return {
                "total_entries": len(self._data),
                "fresh_entries": fresh_count,
                "expired_entries": expired_count,
                "cache_hits": self._hits,
                "cache_misses": self._misses,
                "expired_hits": self._expired_hits,
                "hit_rate": hit_rate,
                "evictions": self._evictions,
                "max_size": self.max_size,
                "ttl_seconds": self.ttl,
                "grace_period_seconds": self.grace_period,
            }

    def health_check(self) -> Dict[str, any]:
        """Health check for monitoring systems."""
        stats = self.get_stats()
        
        # Consider cache healthy if:
        # - Hit rate is reasonable (>50% for established workloads)
        # - Not too many expired entries (suggests provider issues)
        # - Not hitting size limits constantly
        
        is_healthy = (
            stats["hit_rate"] > 0.3 and  # At least 30% hit rate
            stats["expired_entries"] < stats["total_entries"] * 0.5 and  # Less than 50% expired
            stats["total_entries"] < self.max_size * 0.9  # Not near size limit
        )
        
        return {
            "healthy": is_healthy,
            "hit_rate": stats["hit_rate"],
            "expired_ratio": stats["expired_entries"] / max(1, stats["total_entries"]),
            "size_utilization": stats["total_entries"] / self.max_size,
        }
