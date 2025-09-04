from __future__ import annotations
import asyncio
import time
from typing import Optional, Iterable, Dict, Set
from .interface import SecretProvider
from .cache import TTLSecretCache
from .azure_key_vault import AzureKeyVaultSecretProvider
from src.infra.utils.logger import get_logger

logger = get_logger("secrets_manager")


class SecretsManager:
    """High-level facade combining provider + cache + orchestration utilities.

    Responsibilities
    ----------------
    * Provide a *single* place the rest of the codebase calls to obtain a secret.
    * Hide provider selection, caching policy, and potential retries.
    * Offer warming & invalidation primitives (rotation workflows).
    * Production-ready retry logic with exponential backoff.
    * Circuit breaker pattern for Azure Key Vault failures.
    * Enhanced monitoring and logging for production debugging.

    Concurrency Model
    -----------------
    Reads are optimistic: first consult the local cache (cheap, lock in cache
    object). Cache miss triggers a provider call with retry logic. To prevent a 
    thundering herd, we use per-key async locks for concurrent cache misses.

    Circuit Breaker
    ---------------
    Tracks consecutive failures and temporarily disables Azure Key Vault calls
    when failure rate exceeds threshold, falling back to cache-only mode.
    """

    def __init__(
        self, 
        provider: SecretProvider | None = None, 
        ttl_seconds: int = 900,  # Increased from 300 to 900 (15 minutes)
        max_size: int = 1000,
        max_retries: int = 3,
        initial_retry_delay: float = 1.0,
        max_retry_delay: float = 30.0,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: int = 60
    ):
        self.provider = provider or AzureKeyVaultSecretProvider()
        self.cache = TTLSecretCache(ttl_seconds=ttl_seconds, max_size=max_size)
        self._global_lock = asyncio.Lock()
        self._key_locks: Dict[str, asyncio.Lock] = {}
        
        # Retry configuration
        self.max_retries = max_retries
        self.initial_retry_delay = initial_retry_delay
        self.max_retry_delay = max_retry_delay
        
        # Circuit breaker configuration
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_timeout = circuit_breaker_timeout
        self._consecutive_failures = 0
        self._circuit_breaker_open_time: Optional[float] = None
        self._failed_secrets: Set[str] = set()
        
        # Metrics for monitoring
        self._cache_hits = 0
        self._cache_misses = 0
        self._provider_calls = 0
        self._provider_failures = 0

    async def _get_key_lock(self, name: str) -> asyncio.Lock:
        """Get or create a lock for a specific secret key."""
        async with self._global_lock:
            if name not in self._key_locks:
                self._key_locks[name] = asyncio.Lock()
            return self._key_locks[name]

    def _is_circuit_breaker_open(self) -> bool:
        """Check if circuit breaker is currently open."""
        if self._circuit_breaker_open_time is None:
            return False
        
        if time.time() - self._circuit_breaker_open_time > self.circuit_breaker_timeout:
            # Reset circuit breaker after timeout
            self._circuit_breaker_open_time = None
            self._consecutive_failures = 0
            logger.info("Circuit breaker reset after timeout")
            return False
        
        return True

    def _record_failure(self, secret_name: str):
        """Record a provider failure and potentially open circuit breaker."""
        self._consecutive_failures += 1
        self._provider_failures += 1
        self._failed_secrets.add(secret_name)
        
        if self._consecutive_failures >= self.circuit_breaker_threshold:
            self._circuit_breaker_open_time = time.time()
            logger.error(
                f"Circuit breaker opened after {self._consecutive_failures} consecutive failures. "
                f"Will retry after {self.circuit_breaker_timeout} seconds."
            )

    def _record_success(self, secret_name: str):
        """Record a successful provider call."""
        self._consecutive_failures = 0
        self._circuit_breaker_open_time = None
        self._failed_secrets.discard(secret_name)

    async def reset_provider_client(self):
        """Reset the Azure Key Vault client to handle event loop issues."""
        try:
            logger.info("Resetting Azure Key Vault client due to event loop issues")
            if hasattr(self.provider, '_client'):
                self.provider._client = None
            if hasattr(self.provider, '_credential'):
                self.provider._credential = None
            # Reset some failure tracking to allow immediate retry
            self._consecutive_failures = max(0, self._consecutive_failures - 2)
            logger.info("Azure Key Vault client reset completed")
        except Exception as e:
            logger.error(f"Error resetting Azure Key Vault client: {e}")

    async def _retry_with_backoff(self, secret_name: str) -> Optional[str]:
        """Fetch secret from provider with exponential backoff retry."""
        last_exception = None
        event_loop_error_detected = False
        
        for attempt in range(self.max_retries + 1):
            try:
                self._provider_calls += 1
                value = await self.provider.get(secret_name)
                
                if value is not None:
                    self._record_success(secret_name)
                    logger.debug(f"Successfully retrieved secret '{secret_name}' on attempt {attempt + 1}")
                    return value
                else:
                    logger.warning(f"Secret '{secret_name}' not found in provider (attempt {attempt + 1})")
                    
            except Exception as e:
                last_exception = e
                error_msg = str(e).lower()
                
                # Check for event loop errors
                if "event loop is closed" in error_msg or "no running event loop" in error_msg:
                    event_loop_error_detected = True
                    logger.error(f"Event loop error for secret '{secret_name}' on attempt {attempt + 1}: {e}")
                    
                    # Reset the client on first event loop error
                    if attempt == 0:
                        try:
                            await self.reset_provider_client()
                        except Exception as reset_error:
                            logger.error(f"Failed to reset provider client: {reset_error}")
                else:
                    logger.warning(f"Failed to retrieve secret '{secret_name}' on attempt {attempt + 1}: {e}")
                
                if attempt < self.max_retries:
                    delay = min(
                        self.initial_retry_delay * (2 ** attempt),
                        self.max_retry_delay
                    )
                    # Use shorter delay for event loop errors since client reset might fix it quickly
                    if event_loop_error_detected:
                        delay = min(delay, 5.0)  # Max 5 seconds for event loop errors
                    
                    logger.info(f"Retrying secret '{secret_name}' in {delay:.2f} seconds")
                    await asyncio.sleep(delay)
        
        # All retries exhausted
        if event_loop_error_detected:
            logger.error(f"All retries exhausted for secret '{secret_name}' due to event loop errors")
            # Don't count event loop errors as heavily toward circuit breaker
            # since they're infrastructure issues, not Azure Key Vault issues
            self._provider_failures += 1
        else:
            self._record_failure(secret_name)
        
        if last_exception:
            logger.error(f"All retries exhausted for secret '{secret_name}': {last_exception}")
        else:
            logger.error(f"All retries exhausted for secret '{secret_name}': not found")
        
        return None

    async def get(self, name: str) -> Optional[str]:
        """Return secret value with production-ready retry logic and circuit breaker.

        Flow: cache -> circuit breaker check -> provider with retries -> populate cache -> return.
        """
        # Check cache first
        cached = self.cache.get(name)
        if cached is not None:
            self._cache_hits += 1
            logger.debug(f"Cache hit for secret '{name}'")
            return cached
        
        self._cache_misses += 1
        logger.debug(f"Cache miss for secret '{name}'")
        
        # Use per-key lock to prevent duplicate calls for the same secret
        key_lock = await self._get_key_lock(name)
        async with key_lock:
            # Double-check cache after acquiring lock
            cached2 = self.cache.get(name)
            if cached2 is not None:
                self._cache_hits += 1
                logger.debug(f"Cache hit after lock acquisition for secret '{name}'")
                return cached2
            
            # Check circuit breaker
            if self._is_circuit_breaker_open():
                logger.error(
                    f"Circuit breaker is open, cannot fetch secret '{name}' from provider. "
                    f"Using cache-only mode."
                )
                return None
            
            # Fetch from provider with retries
            value = await self._retry_with_backoff(name)
            
            if value is not None:
                self.cache.set(name, value)
                logger.info(f"Successfully cached secret '{name}'")
            
            return value

    async def get_with_fallback(self, name: str, fallback_ttl: int = 3600) -> Optional[str]:
        """Get secret with extended cache fallback for critical operations.
        
        If provider fails, attempts to use expired cache entries within fallback_ttl.
        This is useful for database connections where temporary secret unavailability
        shouldn't cause complete service outage.
        """
        # Try normal get first
        value = await self.get(name)
        if value is not None:
            return value
        
        # Check if we have an expired cache entry within fallback window
        fallback_value = self.cache.get_expired(name, fallback_ttl)
        if fallback_value is not None:
            logger.warning(
                f"Using expired cache for critical secret '{name}' "
                f"(max_age: {fallback_ttl}s)"
            )
            return fallback_value
        
        logger.error(f"No fallback available for critical secret '{name}'")
        return None

    async def invalidate(self, name: str):
        """Remove a cached secret (rotation or forced refresh)."""
        self.cache.invalidate(name)
        # Also remove from failed secrets set to allow retry
        self._failed_secrets.discard(name)
        logger.info(f"Invalidated secret '{name}' from cache and failure tracking")

    async def warm(self, names: Iterable[str]):
        """Preload a collection of secrets concurrently (best effort)."""
        logger.info(f"Warming {len(list(names))} secrets")
        results = await asyncio.gather(*(self.get(n) for n in names), return_exceptions=True)
        
        success_count = sum(1 for r in results if r is not None and not isinstance(r, Exception))
        logger.info(f"Warmed {success_count}/{len(results)} secrets successfully")

    def get_metrics(self) -> Dict[str, any]:
        """Get metrics for monitoring and debugging."""
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": self._cache_hits / max(1, self._cache_hits + self._cache_misses),
            "provider_calls": self._provider_calls,
            "provider_failures": self._provider_failures,
            "consecutive_failures": self._consecutive_failures,
            "circuit_breaker_open": self._is_circuit_breaker_open(),
            "failed_secrets_count": len(self._failed_secrets),
            "cached_secrets_count": len(self.cache._data),
        }

    def health_check(self) -> Dict[str, any]:
        """Health check for monitoring systems."""
        metrics = self.get_metrics()
        is_healthy = (
            not self._is_circuit_breaker_open() and 
            self._consecutive_failures < self.circuit_breaker_threshold // 2
        )
        
        return {
            "healthy": is_healthy,
            "circuit_breaker_open": metrics["circuit_breaker_open"],
            "consecutive_failures": metrics["consecutive_failures"],
            "cache_hit_rate": metrics["cache_hit_rate"],
            "last_failure_count": len(self._failed_secrets),
        }


# Singleton instance with production-ready configuration
secrets_manager = SecretsManager(
    ttl_seconds=900,  # 15 minutes (increased from 5 minutes)
    max_size=2000,    # Increased cache size for multi-tenant workload
    max_retries=3,    # Retry failed requests
    initial_retry_delay=1.0,
    max_retry_delay=30.0,
    circuit_breaker_threshold=5,    # Open circuit breaker after 5 failures
    circuit_breaker_timeout=60      # Keep circuit breaker open for 60 seconds
)
