import os
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI

from app_layer.utils.logger import get_logger

logger = get_logger()


def create_fastapi_app():
    app = FastAPI(title="User auth API", version="1.0.0", debug=True)

    logger.info("Setting Up App Config")
    origins = os.getenv("ALLOW_ORIGIN_URL_AUTH", "*").split(",")

    # Middleware for CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    return app
