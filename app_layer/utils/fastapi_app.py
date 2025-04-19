# app_layer/utils/fastapi_app.py

import os
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

from fastapi import Depends

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from socketio import ASGIApp

from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from core.app_state import fastapp
from core.database import init_master_db, get_session_dependency
from core.redis_client import get_redis_client

from app_layer.routers.healthcheck import healthcheck_router
from app_layer.routers.general_chat import chat_router
import app_layer.websockets.connect_handler

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
    logger.info(f"App Environment: {APP_ENV}")

    # Add SessionMiddleware for session handling
    fastapp.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("SESSION_SECRET", "supersecretkey"),
        session_cookie="my_custom_session_cookie",
        max_age=3600,
        same_site="lax",
        https_only=APP_ENV != "local",
    )

    # Initialize Redis client in app state
    fastapp.state.redis_client = get_redis_client()

    # Initialize master database engine and session
    master_engine = init_master_db(create_if_missing=False)
    fastapp.state.master_db_engine = master_engine
    fastapp.state.master_session = get_session_dependency(master_engine)

    # Include routers here
    fastapp.include_router(healthcheck_router, tags=["healthcheck"])
    fastapp.include_router(chat_router, prefix="/chats", tags=["chats"])

    # Wrap in Socket.IO
    return ASGIApp(sio, other_asgi_app=fastapp)
