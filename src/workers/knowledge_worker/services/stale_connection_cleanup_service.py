"""Stale database connection cleanup service.

This service connects to the master database and terminates connections
that have been idle or active for longer than a threshold (default: 10 minutes).

This runs at Tier 1 level (no organization loop) - it's a global cleanup task
that maintains database health by preventing connection leaks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import psycopg2
from dotenv import load_dotenv

from src.infra.utils.logger import get_logger

load_dotenv(override=True)

logger = get_logger("stale_connection_cleanup_service")

# Default threshold for stale connections (in minutes)
DEFAULT_STALE_THRESHOLD_MINUTES = 10


@dataclass
class ConnectionInfo:
    """Information about a database connection."""

    pid: int
    usename: str
    application_name: str
    state: str
    query: str | None
    query_start: datetime | None
    state_change: datetime | None
    age_minutes: float


@dataclass
class CleanupResult:
    """Result of connection cleanup operation."""

    total_connections_found: int
    stale_connections_found: int
    connections_terminated: int
    remaining_connections: int
    connections_details: list[dict]
    error: str | None = None


class StaleConnectionCleanupService:
    """Service for cleaning up stale database connections.

    A connection is considered stale if it has been in an idle state
    for longer than the threshold, or if it has been running a query
    for longer than the threshold (potential stuck query).

    This service connects to the azure_maintenance database when possible
    to avoid being blocked by pool exhaustion on the main database.
    """

    def __init__(self, threshold_minutes: int = DEFAULT_STALE_THRESHOLD_MINUTES):
        """Initialize the service.

        Args:
            threshold_minutes: Minutes after which a connection is considered stale
        """
        self.threshold_minutes = threshold_minutes
        self.target_db = os.getenv("DATABASE_NAME", "postgres")

    def _get_connection_params(self, use_maintenance_db: bool = False) -> dict:
        """Get database connection parameters from environment."""
        # Use azure_maintenance database if pool is exhausted - it has reserved slots
        dbname = "azure_maintenance" if use_maintenance_db else self.target_db
        return {
            "host": os.getenv("DATABASE_HOST", "localhost"),
            "port": os.getenv("DATABASE_PORT", "5432"),
            "user": os.getenv("DATABASE_USER", "postgres"),
            "password": os.getenv("DATABASE_PASSWORD", ""),
            "dbname": dbname,
            "connect_timeout": 10,
        }

    def _get_connection(self) -> psycopg2.extensions.connection | None:
        """Establish database connection, trying maintenance DB if main is exhausted."""
        for use_maintenance in [False, True]:
            params = self._get_connection_params(use_maintenance_db=use_maintenance)
            db_name = params["dbname"]
            try:
                conn = psycopg2.connect(**params)
                logger.info(
                    "Connected to %s:%s/%s for connection cleanup",
                    params["host"],
                    params["port"],
                    db_name,
                )
                return conn
            except psycopg2.OperationalError as e:
                if "reserved" in str(e) and not use_maintenance:
                    logger.warning(
                        "Connection pool exhausted on %s, trying maintenance database...",
                        db_name,
                    )
                    continue
                logger.error("Failed to connect to database %s: %s", db_name, e)
                return None
        return None

    def cleanup_stale_connections(self) -> CleanupResult:
        """Main entry point - cleanup stale database connections.

        Returns:
            CleanupResult with statistics about the cleanup operation
        """
        conn = self._get_connection()
        if not conn:
            return CleanupResult(
                total_connections_found=0,
                stale_connections_found=0,
                connections_terminated=0,
                remaining_connections=0,
                connections_details=[],
                error="Failed to establish database connection",
            )

        try:
            conn.autocommit = True
            cursor = conn.cursor()

            # Get all active connections to target database (excluding ourselves)
            cursor.execute(
                """
                SELECT
                    pid,
                    usename,
                    application_name,
                    state,
                    query,
                    query_start,
                    state_change,
                    EXTRACT(EPOCH FROM (now() - COALESCE(state_change, query_start, backend_start))) / 60.0 as age_minutes
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid != pg_backend_pid()
                ORDER BY age_minutes DESC NULLS LAST
                """,
                (self.target_db,),
            )

            all_connections = []
            stale_connections = []

            for row in cursor.fetchall():
                pid, usename, app_name, state, query, query_start, state_change, age_minutes = row
                conn_info = ConnectionInfo(
                    pid=pid,
                    usename=usename or "",
                    application_name=app_name or "",
                    state=state or "",
                    query=query,
                    query_start=query_start,
                    state_change=state_change,
                    age_minutes=age_minutes or 0,
                )
                all_connections.append(conn_info)

                # Check if connection is stale (older than threshold)
                # Don't kill superuser connections
                if (
                    age_minutes
                    and age_minutes > self.threshold_minutes
                    and usename not in ("azuresu", "postgres")
                ):
                    stale_connections.append(conn_info)

            total_count = len(all_connections)
            stale_count = len(stale_connections)

            logger.info(
                "Found %d total connections, %d stale (>%d minutes) for database %s",
                total_count,
                stale_count,
                self.threshold_minutes,
                self.target_db,
            )

            # Log stale connections before terminating
            connections_details = []
            for conn_info in stale_connections:
                query_preview = (
                    (conn_info.query[:100] + "...")
                    if conn_info.query and len(conn_info.query) > 100
                    else conn_info.query
                )
                detail = {
                    "pid": conn_info.pid,
                    "user": conn_info.usename,
                    "app": conn_info.application_name,
                    "state": conn_info.state,
                    "age_minutes": round(conn_info.age_minutes, 2),
                    "query_preview": query_preview,
                }
                connections_details.append(detail)
                logger.info(
                    "Terminating stale connection: PID=%s, User=%s, App=%s, State=%s, Age=%.1f min",
                    conn_info.pid,
                    conn_info.usename,
                    conn_info.application_name,
                    conn_info.state,
                    conn_info.age_minutes,
                )

            # Terminate stale connections
            terminated_count = 0
            if stale_connections:
                stale_pids = [c.pid for c in stale_connections]
                cursor.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s
                      AND pid = ANY(%s)
                      AND pid != pg_backend_pid()
                      AND usename NOT IN ('azuresu', 'postgres')
                    """,
                    (self.target_db, stale_pids),
                )
                terminated_count = cursor.rowcount

            # Get remaining connection count
            cursor.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = %s AND pid != pg_backend_pid()
                """,
                (self.target_db,),
            )
            result_row = cursor.fetchone()
            remaining = result_row[0] if result_row else 0

            cursor.close()
            conn.close()

            logger.info(
                "Connection cleanup completed: terminated=%d, remaining=%d",
                terminated_count,
                remaining,
            )

            return CleanupResult(
                total_connections_found=total_count,
                stale_connections_found=stale_count,
                connections_terminated=terminated_count,
                remaining_connections=remaining,
                connections_details=connections_details,
            )

        except Exception as e:
            logger.error("Error during connection cleanup: %s", e)
            if conn:
                conn.close()
            return CleanupResult(
                total_connections_found=0,
                stale_connections_found=0,
                connections_terminated=0,
                remaining_connections=0,
                connections_details=[],
                error=str(e),
            )
