# Terraform Management Egress Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Dev infrastructure planning resilient to Azure management-plane connection resets from the Managed DevOps Pool.

**Architecture:** Enable the existing VNet module's NAT Gateway so both Managed DevOps Pool subnets have deterministic outbound egress. Lower Terraform's plan and apply concurrency in the shared pipeline template, and retry the idempotent apply task after transient connection resets. No Key Vault, database, or shared-key authentication changes are required.

**Tech Stack:** Terraform, AzureRM, Azure DevOps YAML, Azure Managed DevOps Pools.

---

### Task 1: Enable deterministic Dev pool egress

**Files:**
- Modify: `infra/terraform/azure/dev/main.tf:25`

- [x] Change `module.vnet.enable_nat_gateway` from `false` to `true`.
- [x] Confirm the existing VNet module associates the created NAT Gateway with the managed pool subnets.

### Task 2: Reduce Terraform operation concurrency

**Files:**
- Modify: `.azure-pipelines/templates/terraform-env-stage.yml:175`

- [x] Add `-parallelism=5` to the shared `terraform plan` command.
- [x] Add `-parallelism=5` to the approved-plan apply command.
- [x] Configure two retries on the idempotent AzureCLI apply task.
- [x] Keep the existing plan artifact and `-input=false` behavior unchanged.

### Task 3: Validate and publish

**Files:**
- Test: `infra/terraform/azure/dev`

- [x] Run `terraform fmt -check` for the changed Terraform file.
- [x] Run `terraform validate` for the Dev configuration.
- [x] Review the exact diff and confirm no application secrets or unrelated files changed.
- [ ] Commit the two configuration changes and this plan.
- [ ] Push the branch and create an Azure Repos pull request targeting `main`.
