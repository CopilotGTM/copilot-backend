#!/bin/sh
# Load environment variables (if present)
set -a
if [ -f "/app/.env" ]; then
  . "/app/.env"
fi
set +a

# Start Celery worker with proper logging and task discovery
celery -A celery_app worker --loglevel=info --concurrency=1 --pool=solo --logfile=/var/log/celery.log --autoscale=1,1 &

# Start Uvicorn server as the main process
exec uvicorn app_layer.main:app --host 0.0.0.0 --port 80 --workers 1 --log-level info
