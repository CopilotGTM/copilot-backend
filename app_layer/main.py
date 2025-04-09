from app_layer.routers.healthcheck import healthcheck_router
from app_layer.utils.fastapi_app import create_fastapi_app
from app_layer.utils.logger import get_logger

logger = get_logger()

app = create_fastapi_app()

app.include_router(healthcheck_router, tags=["healthcheck"])
