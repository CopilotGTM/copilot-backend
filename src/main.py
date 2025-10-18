# app_layer/main.py

from dotenv import load_dotenv

from .api.fastapi_app import create_fastapi_app

load_dotenv(override=True)

app = create_fastapi_app()
