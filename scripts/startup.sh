#!/bin/bash
# Robust startup script for Helm hook Job.
set -euo pipefail

echo "[startup] Working directory: $(pwd)"
echo "[startup] Listing scripts directory:"; ls -al scripts || true

echo "[startup] Running master database migrations..."
python -m scripts.run_master_migrations || {
	echo "[startup][ERROR] Master migrations failed" >&2
	exit 1
}

echo "[startup] Running organization schema migrations (all orgs to head)..."
python -m scripts.run_org_migrations --all || {
	echo "[startup][ERROR] Organization migrations failed" >&2
	exit 1
}

# Conditionally run super admin deployment only if SUPER_ADMIN_EMAIL present.
if [ -n "${SUPER_ADMIN_EMAIL:-}" ]; then
	echo "[startup] SUPER_ADMIN_EMAIL detected; running super admin initialization..."
	# The deploy_with_super_admin script already runs migrations again; skip duplicate by exporting flag if supported later.
	python -m scripts.admin.deploy_with_super_admin || {
		echo "[startup][ERROR] Super admin deployment failed" >&2
		exit 1
	}
else
	echo "[startup] SUPER_ADMIN_EMAIL not set; skipping super admin initialization."
fi

echo "[startup] Completed successfully."