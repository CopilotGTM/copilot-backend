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

echo "[startup] Ensuring internal CRM integration defaults for all orgs..."
python -m scripts.maintenance.ensure_internal_crm_setup --all || {
    echo "[startup][ERROR] Internal CRM setup failed" >&2
    exit 1
}

echo "[startup] Publishing deal import CSV template to app storage..."
python -m scripts.maintenance.publish_deal_import_template || {
    echo "[startup][ERROR] Deal import template publish failed" >&2
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
