#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# Verify docker-compose.yml exists in the repo root
if [ ! -f "${REPO_ROOT}/docker-compose.yml" ]; then
    echo -e "\033[0;31mERROR: Could not find docker-compose.yml in ${REPO_ROOT}\033[0m"
    echo -e "\033[0;33mPlease run this script from within the copilot-backend repository.\033[0m"
    exit 1
fi

cd "${REPO_ROOT}"

echo -e "\n\033[0;36m=============================================\033[0m"
echo -e "\033[0;36m  Azure Docker Authentication Setup\033[0m"
echo -e "\033[0;36m=============================================\033[0m\n"
echo -e "\033[0;90mWorking directory: ${REPO_ROOT}\033[0m\n"

# Check if Docker is running
if ! docker info >/dev/null 2>&1; then
    echo -e "\033[0;31mERROR: Docker is not running. Please start Docker Desktop first.\033[0m"
    exit 1
fi

echo -e "\033[0;32m[1/5] Checking container status...\033[0m"
if ! docker ps --filter "name=copilot-api" --format "{{.Names}}" | grep -q copilot-api; then
    echo -e "\033[0;33mStarting containers...\033[0m"
    docker compose up -d api
    sleep 10
fi

if ! docker ps --filter "name=copilot-api" --format "{{.Names}}" | grep -q copilot-api; then
    echo -e "\033[0;31mERROR: copilot-api container is not running.\033[0m"
    echo -e "\033[0;33mPlease run 'docker compose up -d api' first.\033[0m"
    exit 1
fi

echo -e "\033[0;90m      Container 'copilot-api' is running.\033[0m"

echo -e "\033[0;32m[2/5] Fixing permissions on azure-data volume...\033[0m"
docker exec -u root copilot-api sh -c "mkdir -p /home/appuser/.azure && chown -R appuser:appuser /home/appuser/.azure && chmod 755 /home/appuser/.azure"
echo -e "\033[0;90m      Permissions fixed.\033[0m"

echo -e "\033[0;32m[3/5] Running Azure login...\033[0m\n"
echo -e "\033[0;33m      Please follow the browser instructions to log in.\033[0m\n"

# Run az login interactively
docker exec -it copilot-api az login

# Check if login was successful
echo -e "\n\033[0;32m[4/5] Verifying authentication...\033[0m"
if ! docker exec copilot-api az account show >/dev/null 2>&1; then
    echo -e "\033[0;31mERROR: Azure login failed. Please try again.\033[0m"
    exit 1
fi

echo -e "\033[0;90m      Successfully authenticated!\033[0m\n"
echo -e "\033[0;36m      Account Details:\033[0m"
docker exec copilot-api az account show --query "{Name:name, User:user.name, Tenant:tenantDisplayName}" -o table

echo -e "\n\033[0;32m[5/5] Restarting services to apply credentials...\033[0m"
docker compose restart

echo -e "\n\033[0;32m=============================================\033[0m"
echo -e "\033[0;32m  Azure Authentication Complete!\033[0m"
echo -e "\033[0;32m=============================================\033[0m\n"
echo -e "\033[0;90mAll containers now have access to Azure credentials.\033[0m"
echo -e "\033[0;90mThe credentials are stored in the 'azure-data' Docker volume.\033[0m\n"
echo -e "\033[0;33mIf you encounter auth issues later, simply run this script again:\033[0m"
echo -e "\033[0;33m  ./scripts/local/development/azure_docker_auth.sh\033[0m\n"
