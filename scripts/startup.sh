#!/bin/sh
# Robust startup script for Helm hook Job.
set -eu

# Enable pipefail when the current shell supports it (bash/zsh).
if [ -n "${BASH_VERSION:-}" ] || [ -n "${ZSH_VERSION:-}" ]; then
    set -o pipefail
fi

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

# Conditionally run super admin initialization only if SUPER_ADMIN_EMAIL present.
if [ -n "${SUPER_ADMIN_EMAIL:-}" ]; then
    echo "[startup] SUPER_ADMIN_EMAIL detected; running super admin initialization..."
    python -m scripts.admin.init_super_admin || {
        echo "[startup][ERROR] Super admin initialization failed" >&2
        exit 1
    }
else
    echo "[startup] SUPER_ADMIN_EMAIL not set; skipping super admin initialization."
fi

echo "[startup] Publishing deal import template to blob storage..."
python -m scripts.maintenance.publish_deal_import_template || {
    echo "[startup][WARNING] Deal import template publishing failed (non-critical)" >&2
}

echo "[startup] Completed successfully."
