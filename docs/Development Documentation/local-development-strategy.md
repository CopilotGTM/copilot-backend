# Local Development Strategy & Best Practices

## Overview

This document outlines the recommended strategy for local development of the Copilot Backend, addressing the challenges of slow feedback loops when testing against a shared Development environment. It also provides context on how SOC 2 compliant organizations typically handle development workflows.

## The Challenge

Currently, the development workflow involves pushing changes to the shared `dev` environment to test them. This introduces significant latency:

1. **Build & Push Time**: Waiting for CI/CD pipelines.
2. **Resource Contention**: Shared environment stability issues.
3. **Slow Iteration**: Small fixes take minutes instead of seconds to verify.

## Industry Standards (SOC 2 & Enterprise)

In large, SOC 2 compliant organizations, the "Push to Dev to Test" pattern is generally discouraged for the inner development loop.

* **Separation of Duties**: Developers often do not have write access to shared environments (Dev/Stage/Prod) directly. Deployments are handled strictly by CI/CD.
* **Local Isolation**: Developers work in isolated local environments (Docker/Local K8s) or ephemeral cloud environments (one namespace per developer).
* **Data Privacy**: Production data is never used in local development. Instead, seeded anonymized data or synthetic data is used.

## Recommended Development Strategies

We recommend a tiered approach. Choose the strategy that fits your current task.

### Strategy A: Docker Compose (Recommended for Feature Development)

For writing code, fixing bugs, and testing business logic, **Docker Compose** is the most efficient tool. It avoids the complexity of Kubernetes networking and resource overhead.

**Concept:**

* Run `Postgres` and `Redis` as containers locally.
* Run the `API` and `Workers` as containers (or debug them directly in your IDE connecting to the local DB/Redis).
* Use the `full` target in the Dockerfile to get a dev-ready image.

**Pros:**

* **Fastest Feedback Loop**: Changes can be hot-reloaded (mounted volumes).
* **Low Overhead**: Lighter than running a K8s cluster.
* **Simple Debugging**: Easy to attach debuggers to processes.

**Cons:**

* Does not replicate Kubernetes-specific configurations (Ingress, KEDA, Helm values).

### Strategy B: Local Kubernetes with Skaffold/Tilt (Recommended for Infrastructure/Integration)

If you need to test Helm charts, KEDA scalers, or complex service interactions, use a local Kubernetes cluster.

**Tools:**

* **Cluster**: Docker Desktop (Kubernetes enabled), Minikube, or Kind.
* **Orchestration**: **Skaffold** or **Tilt**. (Crucial for speed).

**Why Skaffold/Tilt?**
Instead of manually building and pushing images, these tools watch your source code. When you save a file:

1. They detect the change.
2. They sync the file directly into the running container (hot-sync) OR rebuild and redeploy in seconds.
3. They stream logs to your terminal.

**Workflow:**

1. Start Local K8s.
2. Run `skaffold dev`.
3. Edit code -> Auto-sync -> See result.

#### Variation B.2: Local Compute, Remote Data (Your Proposed "Local Resource Group")

This is a powerful variation where you run the **compute** (API, Workers) in your local Kubernetes cluster, but they connect to **real Azure services** (Postgres, Redis, Storage) hosted in a dedicated "Local-Dev" Resource Group.

* **Architecture**: Local K8s Pods -> Internet -> Azure Public Endpoints (Postgres/Redis).
* **Security**: Configure Azure Network Security Groups (NSGs) or Firewalls to **whitelist only your specific IP address**. This is a standard and acceptable practice for development environments, balancing security with ease of access.
* **Skaffold Support**: **Yes, Skaffold supports this 100%.** Skaffold only manages the deployment of your code. It does not care where your database lives. You simply provide the Azure connection strings as Kubernetes Secrets or Helm values to your local cluster.
* **Pros**:
  * **Realism**: You test against real Azure SQL/Redis behavior (not just Docker containers).
  * **Performance**: Offloads the heavy database processing from your laptop to the cloud.
  * **Persistence**: Data persists even if you restart your laptop.
* **Cons**:
  * **Latency**: Database calls will be slower than localhost (milliseconds vs microseconds).
  * **Cost**: You pay for the Azure resources.

### Strategy C: Hybrid (Bridge to Kubernetes)

If your application depends heavily on Azure-only services (e.g., Azure Key Vault, specific AI models not mockable locally) that are hard to replicate locally.

**Tools:**

* **Telepresence** or **Bridge to Kubernetes (VS Code Extension)**.

**Concept:**

* Run the service *locally* on your machine.
* "Bridge" the network so your local service appears to be *inside* the remote Dev cluster.
* Your local service can talk to the remote Redis/DB, and traffic meant for the remote service is routed to your laptop.

**Pros:**

* Access to real cloud resources without mocking.
**Cons:**
* Requires a stable internet connection.
* Can be complex to set up with network policies.

## Proposed Implementation Plan

To move away from "Push to Dev", we should implement **Strategy A (Docker Compose)** for daily logic work and **Strategy B (Skaffold)** for integration testing.

### 1. Create a `docker-compose.yml`

This file should define:

* `postgres`: Local database.
* `redis`: Local message broker.
* `api`: The FastAPI app (mounting `./src` for hot reload).
* `worker-all`: A single container running all workers (or separate services for `worker-chat`, `worker-deal`, etc., if needed).

### 2. Local Configuration

Create a `.env.local` file that points the application to `localhost` for DB and Redis, but uses real Azure credentials for things that must remain in the cloud (like OpenAI API keys), provided this doesn't violate security policies.

### 3. Seed Data

Create a script to seed the local Postgres with enough dummy data to make the app functional (Users, Tenants, basic Deals).

## Summary Recommendation

1. **Stop pushing to Dev for every line of code.**
2. **Adopt Docker Compose** for 90% of your coding tasks. It mimics the *application* environment perfectly.
3. **Use Local K8s (with Skaffold)** only when you are changing Helm charts or K8s configurations.

This approach aligns with SOC 2 by keeping development isolated and ensuring the shared Dev environment remains stable for integration testing.
