import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from src.infra.secrets.manager import secrets_manager
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.scheduler_heartbeat import (
    get_scheduler_health as get_deal_scheduler_health,
)
from src.workers.integrations_worker.scheduler_heartbeat import get_scheduler_health
from src.workers.knowledge_worker.scheduler_heartbeat import (
    get_scheduler_health as get_knowledge_scheduler_health,
)

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
    return {"status": "healthy", "service": "Copilot Backend", "version": "1.0.0"}


async def verify_internal_access(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> bool:
    """
    Verify that the request is authorized to access internal health endpoints.

    Internal health endpoints are protected exclusively by a shared bearer token.
    Source IPs are not a trustworthy app-layer security boundary behind proxies
    or ingress, so they are intentionally not considered here.
    """
    internal_token = os.getenv("INTERNAL_HEALTH_TOKEN")
    if not internal_token:
        logger.error("Internal health endpoint access attempted without INTERNAL_HEALTH_TOKEN set")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Access denied",
                "message": "Health check endpoints are restricted to internal access only",
            },
        )

    if credentials and secrets.compare_digest(credentials.credentials, internal_token):
        return True

    logger.warning("Unauthorized health check access attempt")
    raise HTTPException(
        status_code=403,
        detail={
            "error": "Access denied",
            "message": "Health check endpoints are restricted to internal access only",
        },
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
        integrations_scheduler = get_scheduler_health()
        deal_scheduler = get_deal_scheduler_health()
        knowledge_scheduler = get_knowledge_scheduler_health()
        overall_healthy = (
            overall_healthy
            and integrations_scheduler["healthy"]
            and deal_scheduler["healthy"]
            and knowledge_scheduler["healthy"]
        )
        health_response = {
            "status": "healthy" if overall_healthy else "unhealthy",
            "application": {"status": "healthy", "service": "Copilot Backend"},
            "secret_management": {
                "secret_manager": {"healthy": True, "metrics": metrics},
                "provider": provider_info,
            },
            "integrations_scheduler": integrations_scheduler,
            "deal_scheduler": deal_scheduler,
            "knowledge_scheduler": knowledge_scheduler,
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
            "recommendations": ["Check application logs for detailed error information"],
        }
        raise HTTPException(status_code=503, detail=error_response) from e


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
                "recommendations": ["Check secret management system configuration and logs"],
            },
        ) from e


@healthcheck_router.get("/health/integrations-scheduler")
async def integrations_scheduler_healthcheck(authorized: bool = Depends(verify_internal_access)):
    """Report freshness of fixed integrations schedules."""

    scheduler_health = get_scheduler_health()
    if scheduler_health["healthy"]:
        return scheduler_health

    raise HTTPException(status_code=503, detail=scheduler_health)


@healthcheck_router.get("/health/deal-scheduler")
async def deal_scheduler_healthcheck(authorized: bool = Depends(verify_internal_access)):
    """Report freshness of fixed deal schedules."""

    scheduler_health = get_deal_scheduler_health()
    if scheduler_health["healthy"]:
        return scheduler_health

    raise HTTPException(status_code=503, detail=scheduler_health)


@healthcheck_router.get("/health/knowledge-scheduler")
async def knowledge_scheduler_healthcheck(authorized: bool = Depends(verify_internal_access)):
    """Report freshness of fixed knowledge schedules."""

    scheduler_health = get_knowledge_scheduler_health()
    if scheduler_health["healthy"]:
        return scheduler_health

    raise HTTPException(status_code=503, detail=scheduler_health)
