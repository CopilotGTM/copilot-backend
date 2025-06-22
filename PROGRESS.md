# Project Progress Reference

This file documents the current state of completed infrastructure setup and modules.

## Infra/Modules Completed

### AKS (Azure Kubernetes Service) Module
  - **Purpose:** Deploys a production-ready AKS cluster into a private subnet, supporting full parameterization.
  - **Inputs:**
    - `aks_name` (string): The name of the AKS cluster.
    - `dns_prefix` (string): The DNS prefix for the AKS cluster.
    - `location` (string): Azure region for the cluster.
    - `resource_group_name` (string): Name of the target resource group.
    - `subnet_id` (string): VNet subnet resource ID for node pool.
    - `agent_count` (number, default=2): Number of nodes in the default pool.
    - `vm_size` (string, default="Standard_DS2_v2"): VM SKU for node pool.
    - `env` (string): Environment identifier for tagging.
    - `service_cidr` (string, default="10.1.0.0/16"): Service address space, must not overlap VNet/subnets.
    - `dns_service_ip` (string, default="10.1.0.10"): DNS service IP, must be in `service_cidr`.
  - **Outputs:**
    - `aks_name`: The cluster name.
    - `aks_id`: Resource ID for the AKS cluster.
    - `kube_config`: Raw kubeconfig for API access (sensitive output).
  - **Details:**
    - Uses system-assigned managed identity.
    - Network configulates Azure CNI ("azure" network_plugin), custom service CIDRs.
    - Subnet assignment and resource group links modular.
    - Node pool is individually configured with name, count, VM size, and supports node pool rotation.

### Azure Resource Group Module
  - **Purpose:** Reusable module for creating resource groups with environment-specific naming.
  - **Inputs:**
    - `resource_group_name` (string): Name of the resource group.
    - `location` (string, default="East US"): Azure region.
  - **Outputs:**
    - `resource_group_name`, `resource_group_location`, `resource_group_id`.
  - **Details:**
    - Simple encapsulation for grouping resources by environment and region.

### Azure VNet Module (with Subnet Support)
  - **Purpose:** Standalone VNet module supporting both public and private subnets for secure network segregation.
  - **Inputs:**
    - `vnet_name` (string): Name of the VNet.
    - `address_space` (list(string)): Address ranges for the VNet.
    - `location` (string): Azure region.
    - `resource_group_name` (string): Resource group for VNet/subnets.
    - `public_subnets` (list(string)): CIDRs of public subnets.
    - `private_subnets` (list(string)): CIDRs of private subnets.
    - `env` (string): Environment tag.
  - **Outputs:**
    - `vnet_name`, `vnet_id`, `address_space`, `public_subnet_ids`, `private_subnet_ids`.
  - **Details:**
    - Iteratively creates any number of public/private subnets.
    - All subnet IDs exposed for downstream module consumption.
    - Tags resources with env for clarity.
    - CIDRs are fully parameterized for flexible networking.

### Azure Container Registry (ACR) Module
  - **Purpose:** Manages a private ACR instance for container images.
  - **Inputs:**
    - `acr_name` (string): Registry name.
    - `resource_group_name` (string): Parent resource group.
    - `location` (string): Azure region.
    - `sku` (string, default="Basic"): Registry SKU (Basic, Standard, Premium).
    - `admin_enabled` (bool, default=false): Enable/disable admin access.
  - **Outputs:**
    - `acr_id`: Registry resource ID.
    - `acr_login_server`: Login server URL for Docker push/pull.
    - `acr_admin_username`, `acr_admin_password`: Credentials if admin_enabled, otherwise null.
  - **Details:**
    - Supports admin account toggle for automation/scenario flexibility.

- **Modular Project Structure**: All configuration and modules under `/terraform`.
- **Environment-Aware Naming**: `env` variable for per-environment suffixing throughout resource naming.
## Current State

- Project uses only Terraform modules.
- `terraform.tfvars` used for environment settings and VNet subnets.
- Foundation is ready for new modules (e.g., Redis, AKS, app services).

---

_Last updated: 2025-06-22_