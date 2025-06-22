# Infrastructure Project Goals & Target Architecture

This document outlines the intended final architecture and functional goals for the project.

---

## Target Architecture Diagram

```plaintext
+--------------------------- Azure Resource Group ----------------------------+
|                                                                            |
|    +------------------+                 +-------------------+              |
|    |     VNet         |                 |      Redis        |              |
|    |  (address space) |                 |  (private or      |              |
|    |                  |                 |   public subnet)  |              |
|    |   +--------------------------+     +-------------------+              |
|    |   |  Private Subnet(s)       |                                        |
|    |   |  +-------------------+   |                                        |
|    |   |  |    AKS Cluster    |   |                                        |
|    |   |  |+---------------+  |   |                                        |
|    |   |  || Microservices|<----pubsub------>Redis (from VNet)             |
|    |   |  +---------------+  |   |                                        |
|    |   +-------------------+   |                                        |
|    |   |   Public Subnet(s)    |                                        |
|    |   +----------------------+                                        |
|    +--------------------------+                                        |
|                                                                            |
+----------------------------------------------------------------------------+
```

---

## Detailed Target Architecture: AKS, WebSocket, Redis, Microservices

### 1. Azure Kubernetes Service (AKS)
- Hosted in private subnet(s) within the Azure VNet.
- Nodes and pods are not externally IP'd by default; ingress is managed explicitly.
- Node pools can separate workloads (e.g., web vs. worker).

### 2. AKS Ingress and WebSocket Services
- **Ingress Controller** (e.g., NGINX or Azure Application Gateway) is deployed to a "public-facing" subnet or via load balancer.
- Accepts WebSocket (WS/WSS) connections from clients.
- Routes incoming WS connections to dedicated microservices (stateless or sticky).
- TLS termination at ingress.

### 3. Kubernetes Microservices
- Deployed as Kubernetes Workloads (e.g., Deployments).
- Stateless API, business logic, WebSocket-handling services.
- Each service has internal Service/ClusterIP for inter-microservice communication.
- Access Redis via internal VNet/private endpoint.

### 4. Redis Pub/Sub
- Managed Azure Cache for Redis (best practice) or containerized/VM Redis in private subnet.
- Exposed to AKS via private subnet for low-latency, secured access.
- Used for:
    - Pub/sub patterns (broadcast messages to all microservices)
    - Session management or fast cache if needed.

### 5. Network & Security
- VNet Subnets:
    - **Public**: Hosts ingress (load balancer, application gateway).
    - **Private**: Hosts AKS, Redis.
- NSGs assigned per subnet for strict traffic policy.
- All internal traffic over private VNet.
- Azure Private DNS Zones to ease discovery.

### 6. Typical Flow

```plaintext
Client (Internet)
        |
     [Public Ingress] ===[WebSocket or HTTP]===>
        |
     [AKS Microservice (WebSocket app)]
        |               ^
        |               |
        +----sub/pub----|-----+
             (Redis)    |     | (other microservice may subscribe)
        |               |
    [Other cluster microservices]
```
- Client connects to ingress using WS/WSS.
- Ingress routes to a WebSocket-aware Pod in AKS.
- Microservices use Redis pub/sub to broadcast (or receive) messages.
- All sensitive assets (AKS nodes, Redis) isolated in private subnet.

---


- **Kubernetes Service (AKS)**
  - Create a production-ready Azure Kubernetes Service cluster.
  - Use VNet (with public and private subnets) to isolate cluster nodes and workloads.
  - Enable secure service-to-service networking.

- **Redis Pub/Sub System**
  - Deploy a managed or self-hosted Redis cluster (for pub/sub messaging).
  - Integrate with Kubernetes workloads for in-cluster communication.

- **Microservices Platform**
  - Deploy microservices into AKS using Helm or Terraform.
  - Secure intra-cluster and public endpoints as needed.

- **Modular, Extensible Infrastructure**
  - All major components (Resource Group, VNet, AKS, Redis) implemented as reusable Terraform modules.
  - Support per-environment deployments (dev, staging, prod).

## Staged Milestones

1. **[x] Modular project foundation (Resource Group, VNet, Subnet modules)**
2. [ ] Implement AKS module
3. [ ] Implement Redis module
4. [ ] Terraform Helm/microservice deployment logic
5. [ ] Document workflows, CI/CD, best practices

## Outcome

A scalable, repeatable, modular infrastructure-as-code setup for full stack cloud-native development using AKS, Redis, and microservice design on Azure.

---

_Last updated: 2025-06-21_