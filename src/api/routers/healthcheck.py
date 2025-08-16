from fastapi import APIRouter

from src.infra.utils.logger import get_logger

logger = get_logger()

healthcheck_router = APIRouter()


@healthcheck_router.get("/healthcheck")
async def healthcheck():
    """
    :return:
    """
    # logger.info("Healthcheck")
    return {"status": "Okz Copilot"}
