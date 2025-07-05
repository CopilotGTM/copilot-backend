# app_layer/main.py

from .api.fastapi_app import create_fastapi_app
from dotenv import load_dotenv

load_dotenv(override=True)

app = create_fastapi_app()
