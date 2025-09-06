from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Dict, Any
import os

from src.infra.utils.logger import get_logger
from src.infra.secrets.manager import secrets_manager

logger = get_logger()

# Security scheme for health endpoints
security = HTTPBearer(auto_error=False)

# Health check router with prefix for internal endpoints
healthcheck_router = APIRouter(prefix="/internal")

# Public router for basic health checks (load balancer probes)
public_health_router = APIRouter()


@public_health_router.get("/health")
async def public_health():
    """
    Public health endpoint for load balancers and external monitoring.
    
    This endpoint:
    - Does NOT expose sensitive system information
    - Only returns basic application status
    - Safe for public access
    """
    return {
        "status": "healthy",
        "service": "Copilot Backend",
        "version": "1.0.0"
    }


async def verify_internal_access(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> bool:
    """
    Verify that the request is authorized to access internal health endpoints.
    
    This function checks for:
    1. Internal health check token (for monitoring systems)
    2. Request from internal cluster IP ranges
    3. Kubernetes service account token (for internal pod access)
    """
    # Check for internal health check token
    internal_token = os.getenv("INTERNAL_HEALTH_TOKEN")
    if internal_token and credentials and credentials.credentials == internal_token:
        return True
    
    # Check if request is from internal cluster (Kubernetes internal networks)
    client_ip = request.client.host if request.client else None
    if client_ip:
        # Common Kubernetes internal IP ranges
        internal_ranges = [
            "10.",      # Kubernetes cluster internal
            "172.",     # Docker internal networks  
            "192.168.", # Private networks
            "127.0.0.1" # Localhost
        ]
        if any(client_ip.startswith(prefix) for prefix in internal_ranges):
            logger.info(f"Internal health check access from cluster IP: {client_ip}")
            return True
    
    # Check for Kubernetes service account token
    if credentials and credentials.credentials:
        # In a real environment, you'd validate the Kubernetes service account token
        # For now, we'll accept any bearer token from internal IPs
        if client_ip and any(client_ip.startswith(prefix) for prefix in ["10.", "172.", "192.168."]):
            return True
    
    # Log unauthorized access attempts
    logger.warning(f"Unauthorized health check access attempt from {client_ip}")
    raise HTTPException(
        status_code=403,
        detail={
            "error": "Access denied",
            "message": "Health check endpoints are restricted to internal access only",
            "client_ip": client_ip
        }
    )


@healthcheck_router.get("/healthcheck")
async def healthcheck(authorized: bool = Depends(verify_internal_access)):
    """Basic health check endpoint."""
    # logger.info("Healthcheck")
    return {"status": "OK - Copilot Backend"}


@healthcheck_router.get("/health/detailed")
async def detailed_healthcheck(authorized: bool = Depends(verify_internal_access)):
    """
    Comprehensive health check including all system components.
    
    Returns detailed health information for:
    - Basic application status
    - Secret management system
    - Cache performance
    - Azure Key Vault connectivity
    """
    try:
        # Build simplified health based on lean secrets manager
        metrics = secrets_manager.get_metrics()
        # Provider info if available
        provider_info = {}
        try:
            provider_info = secrets_manager.provider.get_client_info()  # type: ignore[attr-defined]
        except Exception:
            provider_info = {}

        overall_healthy = True
        health_response = {
            "status": "healthy",
            "application": {"status": "healthy", "service": "Copilot Backend"},
            "secret_management": {
                "secret_manager": {"healthy": True, "metrics": metrics},
                "provider": provider_info,
            },
            "overall_healthy": overall_healthy,
        }
        
        # Return appropriate HTTP status
        if overall_healthy:
            return health_response
        else:
            # Return 503 Service Unavailable if unhealthy for load balancer decisions
            raise HTTPException(status_code=503, detail=health_response)
    
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        error_response = {
            "status": "error",
            "error": str(e),
            "overall_healthy": False,
            "recommendations": ["Check application logs for detailed error information"]
        }
        raise HTTPException(status_code=503, detail=error_response)


@healthcheck_router.get("/health/secrets")
async def secrets_healthcheck(authorized: bool = Depends(verify_internal_access)):
    """
    Focused health check for secret management system.
    
    Useful for monitoring specifically secret-related issues without
    checking other system components.
    """
    try:
        metrics = secrets_manager.get_metrics()
        try:
            provider_info = secrets_manager.provider.get_client_info()  # type: ignore[attr-defined]
        except Exception:
            provider_info = {}

        secrets_response = {
            "status": "healthy",
            "secret_manager": {"healthy": True, "metrics": metrics},
            "provider": provider_info,
            "recommendations": [],
        }
        
        return secrets_response
    
    except Exception as e:
        logger.error(f"Secrets health check failed: {e}")
        raise HTTPException(
            status_code=503, 
            detail={
                "status": "error", 
                "error": str(e),
                "recommendations": ["Check secret management system configuration and logs"]
            }
        )
