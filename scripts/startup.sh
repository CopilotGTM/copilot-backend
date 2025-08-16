#!/bin/sh
set -e

echo "Running database migrations..."
python -m scripts.run_master_migrations

echo "Deploying with super admin..."
python -m scripts.admin.deploy_with_super_admin

echo "Startup scripts completed successfully."