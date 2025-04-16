# app_layer/utils/fastapi_app.py

import os
from fastapi.middleware.cors import CORSMiddleware
from socketio import ASGIApp
from starlette.middleware.sessions import SessionMiddleware

from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from core.app_state import fastapp

# Import routers here
from app_layer.routers.healthcheck import healthcheck_router
import app_layer.websockets.connect_handler

# import Utils
from core.redis_client import get_redis_client

logger = get_logger()


def create_fastapi_app():

    logger.info("Setting Up App Config")

    origins = os.getenv("ALLOW_ORIGIN_URL_AUTH", "*").split(",")
    fastapp.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    APP_ENV = os.getenv("ENV", "dev")

    # Add SessionMiddleware for session handling
    fastapp.add_middleware(
        SessionMiddleware,
        secret_key="supersecretkey",
        session_cookie="my_custom_session_cookie",
        max_age=3600,
        same_site="lax",
        https_only=APP_ENV != "local",
    )

    # Initialize Redis client in app state
    fastapp.state.redis_client = get_redis_client()

    # Include routers here
    fastapp.include_router(healthcheck_router, tags=["healthcheck"])

    # Wrap with socketio
    return ASGIApp(sio, other_asgi_app=fastapp)
