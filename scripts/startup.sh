#!/bin/sh
# Robust startup script for Helm hook Job.
set -euo pipefail

echo "[startup] Working directory: $(pwd)"
echo "[startup] Showing contents of scripts/ (this does NOT run them, just lists):"; ls -1 scripts || true

# Show key env presence (not secrets) to aid debugging
echo "[startup] ENV DATABASE_HOST=${DATABASE_HOST:-<unset>} DATABASE_NAME=${DATABASE_NAME:-<unset>} DATABASE_USER=${DATABASE_USER:-<unset>} SUPER_ADMIN_EMAIL=${SUPER_ADMIN_EMAIL:-<unset>}"

# Wait for database readiness (basic connectivity loop)
DB_HOST=${DATABASE_HOST:-localhost}
DB_PORT=${DATABASE_PORT:-5432}
WAIT_TIMEOUT=${DB_WAIT_TIMEOUT_SECONDS:-120}
SLEEP=5
ELAPSED=0
echo "[startup] Waiting for database ${DB_HOST}:${DB_PORT} (timeout ${WAIT_TIMEOUT}s)..."
while :; do
	if python - <<'EOF'
import os, sys, urllib.parse, sqlalchemy
from sqlalchemy import text
host=os.getenv('DATABASE_HOST','localhost')
port=os.getenv('DATABASE_PORT','5432')
name=os.getenv('DATABASE_NAME','postgres')
user=os.getenv('DATABASE_USER','postgres')
pwd=os.getenv('DATABASE_PASSWORD','')
url=f"postgresql://{user}:{urllib.parse.quote_plus(pwd)}@{host}:{port}/{name}"
try:
		eng=sqlalchemy.create_engine(url, future=True, pool_pre_ping=True)
		with eng.connect() as c: c.execute(text('SELECT 1'))
except Exception as e:
		print(f"DB not ready: {e}")
		sys.exit(1)
print('DB ready')
EOF
	then
		echo "[startup] Database connection succeeded."
		break
	fi
	ELAPSED=$((ELAPSED+SLEEP))
	if [ "$ELAPSED" -ge "$WAIT_TIMEOUT" ]; then
		echo "[startup][ERROR] Database not reachable within ${WAIT_TIMEOUT}s" >&2
		exit 1
	fi
	sleep $SLEEP
done

echo "[startup] Running master database migrations..."
python -m scripts.run_master_migrations || {
	echo "[startup][ERROR] Master migrations failed" >&2
	exit 1
}

# Conditionally run super admin deployment only if SUPER_ADMIN_EMAIL present.
if [ -n "${SUPER_ADMIN_EMAIL:-}" ]; then
	echo "[startup] SUPER_ADMIN_EMAIL detected; running super admin initialization..."
	python -m scripts.admin.deploy_with_super_admin || {
		echo "[startup][ERROR] Super admin deployment failed" >&2
		exit 1
	}
else
	echo "[startup] SUPER_ADMIN_EMAIL not set; skipping super admin initialization."
fi

echo "[startup] Completed successfully."