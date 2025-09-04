#!/usr/bin/env python3
"""
Security validation script for health check endpoints.
Verifies that internal endpoints are properly secured.
"""
import requests
import sys
import os
from typing import Dict, Any

def test_public_health_endpoint(base_url: str) -> Dict[str, Any]:
    """Test that public health endpoint works and doesn't expose sensitive data."""
    print("🌐 Testing public health endpoint...")
    
    try:
        response = requests.get(f"{base_url}/health", timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # Check that it doesn't expose sensitive information
            sensitive_keys = ['secret', 'password', 'token', 'key', 'connection', 'database']
            exposed_sensitive = [key for key in sensitive_keys if any(key in str(data).lower() for key in sensitive_keys)]
            
            result = {
                "status": "✅ PASS",
                "accessible": True,
                "response": data,
                "sensitive_data_exposed": exposed_sensitive
            }
            
            if exposed_sensitive:
                result["status"] = "⚠️ WARNING"
                result["warning"] = f"Potentially sensitive data exposed: {exposed_sensitive}"
                
        else:
            result = {
                "status": "❌ FAIL",
                "accessible": False,
                "status_code": response.status_code,
                "response": response.text
            }
            
    except Exception as e:
        result = {
            "status": "❌ ERROR",
            "accessible": False,
            "error": str(e)
        }
    
    return result

def test_internal_health_endpoints(base_url: str) -> Dict[str, Any]:
    """Test that internal health endpoints are properly restricted."""
    print("🔒 Testing internal health endpoint security...")
    
    internal_endpoints = [
        "/internal/healthcheck",
        "/internal/health/detailed", 
        "/internal/health/secrets"
    ]
    
    results = {}
    
    for endpoint in internal_endpoints:
        print(f"  Testing {endpoint}...")
        
        try:
            # Test without authorization
            response = requests.get(f"{base_url}{endpoint}", timeout=10)
            
            if response.status_code == 403:
                results[endpoint] = {
                    "status": "✅ PASS",
                    "properly_restricted": True,
                    "status_code": response.status_code
                }
            elif response.status_code == 200:
                results[endpoint] = {
                    "status": "❌ FAIL",
                    "properly_restricted": False,
                    "status_code": response.status_code,
                    "error": "Internal endpoint accessible without authorization!"
                }
            else:
                results[endpoint] = {
                    "status": "⚠️ UNKNOWN",
                    "properly_restricted": "unknown",
                    "status_code": response.status_code,
                    "note": "Unexpected response code"
                }
                
        except Exception as e:
            results[endpoint] = {
                "status": "❌ ERROR",
                "error": str(e)
            }
    
    return results

def main():
    """Run security validation tests."""
    print("🛡️ Health Check Security Validation")
    print("=" * 50)
    
    # Get base URL from environment or use default
    base_url = os.getenv("API_BASE_URL", "https://devapi.copilotgtm.com")
    print(f"🌐 Testing against: {base_url}")
    print()
    
    # Test public endpoint
    public_result = test_public_health_endpoint(base_url)
    print(f"📊 Public Health Endpoint: {public_result['status']}")
    if public_result.get('accessible'):
        print(f"   Response: {public_result['response']}")
    if public_result.get('sensitive_data_exposed'):
        print(f"   ⚠️ Sensitive data: {public_result['sensitive_data_exposed']}")
    if public_result.get('error'):
        print(f"   ❌ Error: {public_result['error']}")
    print()
    
    # Test internal endpoints
    internal_results = test_internal_health_endpoints(base_url)
    print("🔒 Internal Health Endpoints:")
    
    all_secure = True
    for endpoint, result in internal_results.items():
        print(f"   {endpoint}: {result['status']}")
        if result.get('error'):
            print(f"     ❌ Error: {result['error']}")
            all_secure = False
        elif not result.get('properly_restricted'):
            all_secure = False
    
    print()
    print("📋 Security Summary:")
    
    if public_result['status'].startswith('✅') and all_secure:
        print("✅ All health check endpoints are properly secured!")
        print("   - Public endpoint accessible but doesn't expose sensitive data")
        print("   - Internal endpoints properly restricted")
        return 0
    else:
        print("❌ Security issues detected!")
        if not public_result['status'].startswith('✅'):
            print("   - Public endpoint has issues")
        if not all_secure:
            print("   - Internal endpoints not properly secured")
        return 1

if __name__ == "__main__":
    sys.exit(main())
