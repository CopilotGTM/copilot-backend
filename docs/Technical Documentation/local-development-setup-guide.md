# Local Development Environment Setup Guide

## 1. Overview

This document outlines the procedures for configuring and executing the Copilot Backend application in a local development environment. The architecture utilizes a hybrid approach:

* **Compute**: Application services (API, Workers) run locally within a Kubernetes cluster (Docker Desktop) orchestrated by Skaffold.
* **Database**: Connects to a persistent Azure PostgreSQL instance (`copilotgtm-postgresdb-local`) hosted in the `copilot-gtm-app-local` Resource Group.
* **Cache**: Connects to a local Redis instance running on the host machine.

## 2. Prerequisites

Ensure the following software is installed and configured before proceeding:

* **Docker Desktop**: Kubernetes must be enabled in settings.
* **Skaffold**: [Installation Guide](https://skaffold.dev/docs/install/) or `winget install -e --id Google.ContainerTools.Skaffold`.
* **Azure CLI**: Authenticated via `az login`.
* **Kubectl**: Configured to use the `docker-desktop` context.
* **Redis**: A local Redis server running on port 6379.

## 3. Infrastructure Configuration

### 3.1. Azure Resources (PostgreSQL)

The application depends on the `copilotgtm-postgresdb-local` PostgreSQL server located in the `copilot-gtm-app-local` resource group.

**Action Required: Network Whitelisting**
To allow the local application to connect to the Azure database, the developer's public IP address must be whitelisted.

1. Navigate to the **Azure Portal**.
2. Locate the **PostgreSQL Flexible Server** resource (`copilotgtm-postgresdb-local`).
3. Select **Networking** from the sidebar.
4. Add the current public IP address to the firewall rules.
5. *Note: This step must be repeated if the public IP address changes.*

### 3.2. Local Resources (Redis)

The application requires a Redis instance for task queuing and caching.

1. Ensure a local Redis server is running on port `6379`.
2. Verify connectivity: `redis-cli ping` should return `PONG`.

## 4. Application Configuration

### 4.1. Environment Variables

The application configuration is derived from the `.env` file located in the project root.

1. Verify the `.env` file exists.
2. Ensure `DATABASE_HOST` points to the Azure PostgreSQL instance (e.g., `copilotgtm-postgresdb-local.postgres.database.azure.com`).
3. Ensure `REDIS_HOST` is set to `localhost`.

### 4.2. Kubernetes Secrets Generation

A helper script is provided to generate the necessary Kubernetes secrets from the `.env` file. This script automatically maps `localhost` to `host.docker.internal` to ensure connectivity from within the Kubernetes cluster.

**Execution:**
Run the following command in a PowerShell terminal:

```powershell
./scripts/setup_local_k8s.ps1
```

**Verification:**
Confirm the secret was created successfully:

```powershell
kubectl get secret copilot-backend-secret
```

### 4.3. Helm Value Overrides

The `values-local.yaml` file contains configuration overrides specific to the local environment.

* **Redis**: Configured to connect to `host.docker.internal:6379`.
* **Ingress**: Disabled in favor of direct port forwarding or LoadBalancer.
* **Scaling**: Replica counts are reduced to minimize resource consumption.

## 5. Execution

### 5.1. Starting the Environment

Initiate the development loop using Skaffold. This command will build the Docker images, deploy the Helm charts, and stream logs to the terminal.

```bash
skaffold dev
```

### 5.2. Development Workflow

* **Code Changes**: Skaffold monitors the source code. Saving a file triggers an automatic sync to the running containers.
* **Rebuilds**: Changes to `Dockerfile` or dependency files will trigger an automatic image rebuild and redeployment.

### 5.3. Stopping the Environment

To stop the application and clean up local Kubernetes resources:

* Press `Ctrl+C` in the terminal running Skaffold.
* Alternatively, run `skaffold delete`.

## 6. Troubleshooting

| Issue | Potential Cause | Resolution |
| :--- | :--- | :--- |
| **Database Connection Timeout** | Firewall restrictions | Verify your public IP is whitelisted in the Azure PostgreSQL Networking settings. |
| **Redis Connection Refused** | Local Redis not running | Ensure the local Redis server is active on port 6379. |
| **CrashLoopBackOff** | Missing Secrets | Re-run `./scripts/setup_local_k8s.ps1` to ensure secrets are up to date. |
