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

from ..api.events.socket_app import sio
from ..infra.utils.logger import get_logger
from ..infra.utils.app_state import fastapp
from ..infra.utils.database import init_master_db, get_session_dependency
from ..infra.messaging.redis_client import get_redis_client

from src.api.routers.healthcheck import healthcheck_router
from src.api.routers.chat_handler import chat_router
from src.api.routers.upload_handler import upload_router
from src.api.routers.technical_deck.transcript_format import transcript_router
from src.api.routers.technical_deck.get_technical_deck import technical_deck
from src.api.routers.technical_deck.select_template import select_template
from src.api.routers.rfp_sheet.sheet_identification import rfp__sheet_router
from src.api.routers.auth_handler import auth_router
from src.api.routers.admin.organizations_handler import admin_org_router
from src.api.routers.admin.migrations_handler import migration_router
from src.api.routers.admin.admin_auth_handler import router as admin_auth_router

from src.api.events import chat_message_handler
from src.api.events import connect_handler
from src.api.events.technical_deck import transcript_summary_handler
from src.api.events.technical_deck import slide_outline_generation
from src.api.events.technical_deck import technical_deck_generation
from src.api.events.rfp_sheet import rfp_answer_generation
from src.api.events import redis_subscriber

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

    # App Level Variables
    fastapp.state.CLOUD_PROVIDER = os.getenv("CLOUD_PROVIDER")
    fastapp.state.APPLICATION_STORAGE_NAME = os.getenv("APPLICATION_STORAGE_NAME")
    fastapp.state.APPLICATION_REGION = os.getenv("APPLICATION_REGION")

    # Include routers here
    fastapp.include_router(healthcheck_router, tags=["healthcheck"])
    fastapp.include_router(auth_router, prefix="/auth", tags=["authentication"])
    fastapp.include_router(chat_router, prefix="/chats", tags=["chats"])
    fastapp.include_router(upload_router, prefix="/chats/upload", tags=["uploads"])
    fastapp.include_router(transcript_router, prefix="/chats/transcript", tags=["transcript"])
    fastapp.include_router(technical_deck, prefix="/chats/technical_deck", tags=["technical_deck"])
    fastapp.include_router(select_template, prefix="/chats/technical_deck", tags=["technical_deck", "select_template"])
    fastapp.include_router(rfp__sheet_router, prefix="/chats/rfp_sheet", tags=["rfp sheet", "sheet identification"])

    # Admin routers
    fastapp.include_router(admin_auth_router, prefix="/admin/auth", tags=["admin", "authentication"])
    fastapp.include_router(admin_org_router, prefix="/admin/organizations", tags=["admin", "organizations"])
    fastapp.include_router(migration_router, prefix="/admin/migrations", tags=["admin", "migrations"])

    # Wrap in Socket.IO
    return ASGIApp(sio, other_asgi_app=fastapp)
