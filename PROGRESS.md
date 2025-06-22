# Project Progress Reference

This file documents the current state of completed infrastructure setup and modules.

## Infra/Modules Completed

- **AKS (Azure Kubernetes Service) Module**
    - Fully parameterized module for production-ready AKS cluster.
    - Cluster deployed in isolated private subnet.
    - User-configurable node count, VM size, DNS prefix.
    - Uses system-assigned managed identity.
    - Wired to consume outputs from vnet and resource group modules.
    - Outputs cluster credentials (kubeconfig), cluster name, and ID for use by developers and automation.

- **Modular Project Structure**: All configuration and modules under `/terraform`.
- **Environment-Aware Naming**: `env` variable for per-environment suffixing throughout resource naming.
- **Azure Resource Group Module**: Modular, reusable.
    - Name includes environment suffix.
    - Output of name, location, and ID supported.
- **Azure VNet Module** (with Subnet support):
    - VNet creation with variable address space.
    - Automatic public/private subnet creation (CIDRs parametric).
    - **Two private subnets explicitly defined**:
        - One for AKS/microservices workloads (`10.0.2.0/24`)
        - One for data services like Redis/DB (`10.0.3.0/24`)
    - Subnet IDs exposed as outputs for downstream resources.

## Current State

- Project uses only Terraform modules.
- `terraform.tfvars` used for environment settings and VNet subnets.
- Foundation is ready for new modules (e.g., Redis, AKS, app services).

---

_Last updated: 2025-06-21_