#!/usr/bin/env python3
"""
Secret Management System Health Monitor

This script provides command-line monitoring and diagnostics for the 
secret management system. Can be run manually or integrated into 
monitoring systems.

Usage:
    python scripts/monitor_secrets.py --health
    python scripts/monitor_secrets.py --metrics
    python scripts/monitor_secrets.py --test-secret <secret-name>
"""

import asyncio
import sys
import argparse
import json
from typing import Dict, Any
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.infra.secrets.manager import secrets_manager
from src.infra.secrets.maintenance import SecretHealthChecker
from src.infra.utils.logger import get_logger

logger = get_logger("secrets_monitor")


async def check_health() -> Dict[str, Any]:
    """Perform comprehensive health check."""
    print("🔍 Performing comprehensive health check...")
    
    health_data = await SecretHealthChecker.comprehensive_health_check()
    
    # Print results
    status = "✅ HEALTHY" if health_data["overall_healthy"] else "❌ UNHEALTHY"
    print(f"\n{status}")
    print(f"Timestamp: {health_data['timestamp']}")
    
    # Secret Manager
    sm_data = health_data["components"]["secret_manager"]
    print(f"\n📊 Secret Manager:")
    print(f"  Healthy: {'✅' if sm_data['healthy'] else '❌'}")
    print(f"  Cache Hit Rate: {sm_data['metrics']['cache_hit_rate']:.2%}")
    print(f"  Consecutive Failures: {sm_data['metrics']['consecutive_failures']}")
    print(f"  Circuit Breaker Open: {'⚠️ YES' if sm_data['metrics']['circuit_breaker_open'] else '✅ NO'}")
    
    # Cache
    cache_data = health_data["components"]["cache"]
    print(f"\n💾 Cache:")
    print(f"  Healthy: {'✅' if cache_data['healthy'] else '❌'}")
    print(f"  Total Entries: {cache_data['stats']['total_entries']}")
    print(f"  Fresh Entries: {cache_data['stats']['fresh_entries']}")
    print(f"  Expired Entries: {cache_data['stats']['expired_entries']}")
    print(f"  Hit Rate: {cache_data['stats']['hit_rate']:.2%}")
    
    # Connectivity
    conn_data = health_data["components"]["connectivity"]
    print(f"\n🌐 Connectivity:")
    print(f"  Test Completed: {'✅' if conn_data['test_completed'] else '❌'}")
    if conn_data["test_error"]:
        print(f"  Test Error: {conn_data['test_error']}")
    
    # Recommendations
    print(f"\n💡 Recommendations:")
    for rec in health_data["recommendations"]:
        print(f"  • {rec}")
    
    return health_data


async def show_metrics():
    """Show detailed metrics."""
    print("📈 Secret Management Metrics\n")
    
    # Secret Manager Metrics
    sm_metrics = secrets_manager.get_metrics()
    print("🔐 Secret Manager:")
    for key, value in sm_metrics.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.3f}")
        else:
            print(f"  {key}: {value}")
    
    # Cache Stats
    cache_stats = secrets_manager.cache.get_stats()
    print(f"\n💾 Cache Statistics:")
    for key, value in cache_stats.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.3f}")
        else:
            print(f"  {key}: {value}")


async def test_secret(secret_name: str):
    """Test fetching a specific secret."""
    print(f"🧪 Testing secret fetch: {secret_name}")
    
    try:
        # Test normal fetch
        print("  Attempting normal fetch...")
        value = await secrets_manager.get(secret_name)
        
        if value:
            print(f"  ✅ Success: Secret found (length: {len(value)})")
        else:
            print("  ❌ Secret not found or failed to fetch")
            
            # Test fallback fetch
            print("  Attempting fallback fetch...")
            fallback_value = await secrets_manager.get_with_fallback(secret_name)
            
            if fallback_value:
                print(f"  ⚠️ Fallback Success: Found in expired cache (length: {len(fallback_value)})")
            else:
                print("  ❌ Fallback also failed")
    
    except Exception as e:
        print(f"  ❌ Error: {e}")


async def run_diagnostics():
    """Run full diagnostic suite."""
    print("🔧 Running Secret Management Diagnostics\n")
    
    # 1. Health Check
    print("=" * 50)
    await check_health()
    
    # 2. Metrics
    print("\n" + "=" * 50)
    await show_metrics()
    
    # 3. Provider Info
    print("\n" + "=" * 50)
    print("🏭 Provider Information:")
    provider_info = secrets_manager.provider.get_client_info()
    for key, value in provider_info.items():
        print(f"  {key}: {value}")


def main():
    parser = argparse.ArgumentParser(description="Secret Management System Monitor")
    parser.add_argument("--health", action="store_true", help="Run health check")
    parser.add_argument("--metrics", action="store_true", help="Show metrics")
    parser.add_argument("--test-secret", type=str, help="Test fetching a specific secret")
    parser.add_argument("--diagnostics", action="store_true", help="Run full diagnostics")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    
    args = parser.parse_args()
    
    if not any([args.health, args.metrics, args.test_secret, args.diagnostics]):
        parser.print_help()
        return
    
    async def run():
        try:
            if args.diagnostics:
                await run_diagnostics()
            elif args.health:
                health_data = await check_health()
                if args.json:
                    print(json.dumps(health_data, indent=2))
            elif args.metrics:
                await show_metrics()
            elif args.test_secret:
                await test_secret(args.test_secret)
        except Exception as e:
            logger.error(f"Monitor script error: {e}")
            print(f"❌ Error: {e}")
            sys.exit(1)
    
    # Run the async function
    asyncio.run(run())


if __name__ == "__main__":
    main()
