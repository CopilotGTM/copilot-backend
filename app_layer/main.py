# app_layer/main.py

from app_layer.utils.fastapi_app import create_fastapi_app
from app_layer.utils.logger import get_logger
from dotenv import load_dotenv

load_dotenv(override=True)

logger = get_logger()
app = create_fastapi_app()
