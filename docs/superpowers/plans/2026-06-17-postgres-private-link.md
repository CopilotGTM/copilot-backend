# PostgreSQL Private Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional Azure Private Link for PostgreSQL Flexible Server, enabled in prod and disabled in dev.

**Architecture:** The shared PostgreSQL Terraform module owns the private endpoint and public access switch. Each environment owns its Private DNS zone and VNet link so the normal database hostname resolves privately inside that environment's VNet. When enabled, Terraform removes the broad public firewall rule and disables PostgreSQL public network access.

**Tech Stack:** Terraform, Azure PostgreSQL Flexible Server, Azure Private Endpoint, Azure Private DNS, AKS VNet.

---

### Task 1: Make PostgreSQL Private Link Optional in the Shared Module

**Files:**
- Modify: `infra/terraform/azure/modules/postgresql/variables.tf`
- Modify: `infra/terraform/azure/modules/postgresql/main.tf`
- Modify: `infra/terraform/azure/modules/postgresql/outputs.tf`

- [ ] **Step 1: Add module inputs**

Add `private_endpoint_enabled`, `private_endpoint_subnet_id`, and `private_dns_zone_ids` variables. Default `private_endpoint_enabled` to `false` so dev and any future environment stay public unless they opt in.

- [ ] **Step 2: Switch public access and firewall by flag**

Set `public_network_access_enabled = !var.private_endpoint_enabled`. Put `count = var.private_endpoint_enabled ? 0 : 1` on the existing `allow-all-ips` firewall rule so prod no longer exposes PostgreSQL publicly after the private endpoint is created.

- [ ] **Step 3: Create private endpoint when enabled**

Create `azurerm_private_endpoint` with `subresource_names = ["postgresqlServer"]`. Attach the private DNS zone group when `private_dns_zone_ids` is provided.

- [ ] **Step 4: Add outputs**

Expose private endpoint ID and private IP for post-deploy verification.

### Task 2: Wire Environment DNS and Feature Flags

**Files:**
- Modify: `infra/terraform/azure/dev/variables.tf`
- Modify: `infra/terraform/azure/prod/variables.tf`
- Modify: `infra/terraform/azure/dev/main.tf`
- Modify: `infra/terraform/azure/prod/main.tf`
- Modify: `infra/terraform/azure/dev/terraform.tfvars`
- Modify: `infra/terraform/azure/prod/terraform.tfvars`

- [ ] **Step 1: Add environment flag**

Add `enable_postgresql_private_endpoint` to dev and prod variables with default `false`.

- [ ] **Step 2: Add PostgreSQL private DNS zone**

Create `azurerm_private_dns_zone.postgresql` with name `privatelink.postgres.database.azure.com` only when the flag is true.

- [ ] **Step 3: Link DNS to the VNet**

Create `azurerm_private_dns_zone_virtual_network_link.postgresql` only when the flag is true.

- [ ] **Step 4: Pass Private Link inputs to the database module**

Pass `private_endpoint_enabled`, `private_endpoint_subnet_id`, and the PostgreSQL private DNS zone ID to `module.application_database`.

- [ ] **Step 5: Enable prod only**

Set `enable_postgresql_private_endpoint = false` in dev tfvars and `true` in prod tfvars.

### Task 3: Validate

**Files:**
- Validate: `infra/terraform/azure/dev`
- Validate: `infra/terraform/azure/prod`

- [ ] **Step 1: Format Terraform**

Run: `terraform fmt -recursive infra/terraform/azure`

- [ ] **Step 2: Validate dev**

Run: `terraform -chdir=infra/terraform/azure/dev validate`

Expected: Success. Dev should not plan PostgreSQL Private Link resources because the flag is false.

- [ ] **Step 3: Validate prod**

Run: `terraform -chdir=infra/terraform/azure/prod validate`

Expected: Success. Prod should support one PostgreSQL private endpoint and no broad PostgreSQL public firewall rule.

### Rollout Notes

- Prod backend should continue using the same database hostname from the variable group. Inside the VNet, Private DNS resolves that hostname to the private endpoint.
- After prod infra deploy, verify from an AKS pod that the database hostname resolves to a private IP and TCP connects to PostgreSQL.
- Direct public access from a laptop or any external tool should stop working after public network access is disabled.
- If a migration or emergency script needs DB access, run it from AKS, a VNet-connected Managed DevOps pool, or another VNet-connected host.
