"""Secret management maintenance tasks for production reliability.

This module provides background tasks and utilities for maintaining
the health of the secret management system.
"""
from __future__ import annotations
import asyncio
import time
from typing import Dict, Any
from .manager import secrets_manager
from src.infra.utils.logger import get_logger

logger = get_logger("secrets_maintenance")


class SecretMaintenanceManager:
    """Manages background maintenance tasks for secret management system."""
    
    def __init__(self, check_interval: int = 300):  # 5 minutes
        self.check_interval = check_interval
        self._running = False
        self._task: asyncio.Task | None = None
    
    async def start(self):
        """Start the maintenance background task."""
        if self._running:
            logger.warning("Maintenance manager already running")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._maintenance_loop())
        logger.info(f"Started secret maintenance manager with {self.check_interval}s interval")
    
    async def stop(self):
        """Stop the maintenance background task."""
        if not self._running:
            return
        
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        logger.info("Stopped secret maintenance manager")
    
    async def _maintenance_loop(self):
        """Main maintenance loop that runs periodically."""
        while self._running:
            try:
                await self._perform_maintenance()
            except Exception as e:
                logger.error(f"Error in maintenance loop: {e}")
            
            # Wait for next interval
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
    
    async def _perform_maintenance(self):
        """Perform all maintenance tasks."""
        logger.debug("Running secret management maintenance")
        
        # Clean up expired cache entries
        secrets_manager.cache.cleanup_expired()
        
        # Log health metrics
        await self._log_health_metrics()
        
        # Check for persistent failures and alert
        await self._check_persistent_failures()
    
    async def _log_health_metrics(self):
        """Log current health metrics for monitoring."""
        try:
            metrics = secrets_manager.get_metrics()
            health = secrets_manager.health_check()
            cache_health = secrets_manager.cache.health_check()
            
            logger.info(
                f"Secret Manager Health: healthy={health['healthy']}, "
                f"cache_hit_rate={metrics['cache_hit_rate']:.2%}, "
                f"consecutive_failures={metrics['consecutive_failures']}, "
                f"circuit_breaker_open={metrics['circuit_breaker_open']}"
            )
            
            if not health['healthy']:
                logger.warning(
                    f"Secret Manager unhealthy: "
                    f"circuit_breaker={health['circuit_breaker_open']}, "
                    f"failures={health['consecutive_failures']}, "
                    f"failed_secrets={metrics['failed_secrets_count']}"
                )
            
            if not cache_health['healthy']:
                logger.warning(
                    f"Secret Cache unhealthy: "
                    f"hit_rate={cache_health['hit_rate']:.2%}, "
                    f"expired_ratio={cache_health['expired_ratio']:.2%}, "
                    f"size_utilization={cache_health['size_utilization']:.2%}"
                )
        
        except Exception as e:
            logger.error(f"Error logging health metrics: {e}")
    
    async def _check_persistent_failures(self):
        """Check for persistent failures that may need intervention."""
        try:
            metrics = secrets_manager.get_metrics()
            
            # Alert on persistent circuit breaker state
            if metrics['circuit_breaker_open']:
                logger.error(
                    "ALERT: Secret Manager circuit breaker has been open. "
                    "This indicates persistent Azure Key Vault connectivity issues. "
                    "Check network connectivity, authentication, and Azure service status."
                )
            
            # Alert on high failure rate
            if metrics['consecutive_failures'] >= 5:
                logger.error(
                    f"ALERT: {metrics['consecutive_failures']} consecutive secret fetch failures. "
                    f"Failed secrets: {metrics['failed_secrets_count']}. "
                    "This may indicate authentication or network issues."
                )
            
            # Alert on poor cache performance
            if metrics['cache_hit_rate'] < 0.3 and metrics['cache_hits'] + metrics['cache_misses'] > 100:
                logger.warning(
                    f"Cache performance is poor: {metrics['cache_hit_rate']:.2%} hit rate. "
                    "Consider investigating TTL settings or secret access patterns."
                )
        
        except Exception as e:
            logger.error(f"Error checking persistent failures: {e}")


class SecretHealthChecker:
    """Health checker for secret management system."""
    
    @staticmethod
    async def comprehensive_health_check() -> Dict[str, Any]:
        """Perform a comprehensive health check of the secret management system."""
        try:
            # Get basic health
            manager_health = secrets_manager.health_check()
            manager_metrics = secrets_manager.get_metrics()
            cache_health = secrets_manager.cache.health_check()
            cache_stats = secrets_manager.cache.get_stats()
            
            # Test a dummy secret fetch (if we have a test secret)
            test_secret = None
            test_error = None
            try:
                # Try to fetch a non-existent secret to test connectivity
                # This should fail gracefully without throwing
                test_secret = await secrets_manager.get("health-check-dummy-secret-that-should-not-exist")
            except Exception as e:
                test_error = str(e)
            
            # Determine overall health
            overall_healthy = (
                manager_health['healthy'] and 
                cache_health['healthy'] and
                manager_metrics['consecutive_failures'] < 3
            )
            
            return {
                "timestamp": time.time(),
                "overall_healthy": overall_healthy,
                "components": {
                    "secret_manager": {
                        "healthy": manager_health['healthy'],
                        "metrics": manager_metrics,
                        "details": manager_health
                    },
                    "cache": {
                        "healthy": cache_health['healthy'],
                        "stats": cache_stats,
                        "details": cache_health
                    },
                    "connectivity": {
                        "test_completed": True,
                        "test_error": test_error,
                        "notes": "Tested with non-existent secret to verify connectivity"
                    }
                },
                "recommendations": SecretHealthChecker._generate_recommendations(
                    manager_health, manager_metrics, cache_health, cache_stats
                )
            }
        
        except Exception as e:
            return {
                "timestamp": time.time(),
                "overall_healthy": False,
                "error": str(e),
                "recommendations": ["Check secret management system logs for errors"]
            }
    
    @staticmethod
    def _generate_recommendations(
        manager_health: Dict[str, Any],
        manager_metrics: Dict[str, Any], 
        cache_health: Dict[str, Any],
        cache_stats: Dict[str, Any]
    ) -> list[str]:
        """Generate actionable recommendations based on health status."""
        recommendations = []
        
        if not manager_health['healthy']:
            if manager_health['circuit_breaker_open']:
                recommendations.append(
                    "Circuit breaker is open - check Azure Key Vault connectivity and authentication"
                )
            if manager_health['consecutive_failures'] >= 3:
                recommendations.append(
                    "Multiple consecutive failures - verify Azure credentials and network connectivity"
                )
        
        if not cache_health['healthy']:
            if cache_health['hit_rate'] < 0.5:
                recommendations.append(
                    "Low cache hit rate - consider increasing TTL or investigating access patterns"
                )
            if cache_health['expired_ratio'] > 0.5:
                recommendations.append(
                    "High expired entry ratio - check if secret provider is experiencing issues"
                )
            if cache_health['size_utilization'] > 0.8:
                recommendations.append(
                    "Cache near size limit - consider increasing max_size or implementing cleanup"
                )
        
        if manager_metrics['provider_failures'] > manager_metrics['provider_calls'] * 0.1:
            recommendations.append(
                "High provider failure rate - investigate Azure Key Vault service status"
            )
        
        if not recommendations:
            recommendations.append("System is healthy - no immediate action required")
        
        return recommendations


# Global maintenance manager instance
maintenance_manager = SecretMaintenanceManager()

# Export public interface
__all__ = ["SecretMaintenanceManager", "SecretHealthChecker", "maintenance_manager"]
