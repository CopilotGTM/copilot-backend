# app_layer/main.py

from app_layer.utils.fastapi_app import create_fastapi_app
from app_layer.utils.logger import get_logger

logger = get_logger()
app = create_fastapi_app()
