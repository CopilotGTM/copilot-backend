# SOC 2 Infrastructure Readiness Plan (Azure)

## Purpose

This document translates the SOC 2 Trust Service Criteria into concrete infrastructure controls for CopilotGTM. It is based on `docs/soc2-compliance-guide.md` and the current Terraform in `terraform/azure/dev` and `terraform/azure/dev/k8s`. The goal is to move from a dev-grade setup to a production-grade, SOC 2 audit-ready infrastructure.

## Scope and assumptions

- Scope: Azure infrastructure and platform controls that can be implemented via Terraform or platform configuration.
- In scope: Networking, identity, secrets, logging/monitoring, data services, AKS/App Service, CI/CD controls.
- Out of scope (still required for SOC 2): HR/security training, incident response process, vendor risk management, legal/privacy policies.
- This document tracks "Implemented?" status based on the current dev Terraform codebase only.

## How to read this

Each control lists:

- Why: SOC 2 rationale
- How: Implementation approach
- Options: Feasible choices
- Best option: Recommended for production
- Implemented?: Yes / Partial / No (with evidence)
- Priority: P0 (blocker), P1 (high), P2 (later)

## Current state snapshot (dev)

Provisioned (dev):

- Resource group, VNet (public + private subnets + private endpoint subnet + PostgreSQL delegated subnet), NAT Gateway, NSG
- AKS with OIDC/workload identity
- ACR, App Services (frontend + admin), Key Vaults
- Postgres Flexible Server, Redis Cache, Storage Accounts
- DevOps pipelines, variable group, service connections
- AKS add-ons: ingress-nginx, cert-manager, KEDA

Notable remaining gaps:

- Public access enabled for Postgres, ACR, and storage/Redis defaults
- Postgres firewall rules are not locked down; no private endpoints
- No centralized logging, diagnostics, or security monitoring
- Secrets appear in `terraform/azure/dev/terraform.tfvars`
- NSG allows inbound 80/443 and NodePort ranges (30000-32767) to private subnets
- No WAF/DDoS protections or private AKS API server
- No HA/zone redundancy for DB, Redis, App Service, or AKS (options now available in modules)

Recently completed (dev):

- Key Vault: Private Endpoints enabled + public network access disabled + Private DNS zone linked to VNet (applied 2025-12-22)

## SOC 2 control register

### Security (CC)

#### CC-1: Human access management (MFA, least privilege, PIM)

- Why: Prevent unauthorized access and reduce blast radius.
- How: Use Entra ID groups, conditional access, MFA, PIM, and scoped RBAC at RG/resource level.
- Options:
  - Direct user assignments (high risk)
  - Group-based RBAC with MFA
  - Group-based RBAC + PIM + conditional access
- Best option: Group-based RBAC + PIM + conditional access + break-glass accounts.
- Implemented?: No (not defined in Terraform).
- Priority: P0

#### CC-2: Least privilege for CI/CD identities

- Why: CI/CD has broad access; abuse impacts the whole subscription.
- How: Replace subscription-level Contributor with scoped roles per pipeline.
- Options:
  - Subscription Contributor (current)
  - Resource group Contributor
  - Custom roles (ACR push/pull, AKS deploy, Key Vault read)
- Best option: Custom roles per pipeline + separate managed identities.
- Implemented?: No. `terraform/azure/modules/service_principal/main.tf` grants Contributor at subscription scope.
- Priority: P0

#### CC-3: Workload identity for AKS

- Why: Eliminate long-lived secrets; use federated identity.
- How: AKS OIDC issuer + AAD federated credentials + scoped RBAC for Key Vault/Storage.
- Options:
  - Kubernetes secrets (avoid)
  - Node managed identity
  - Workload identity (current)
- Best option: Workload identity with least privilege and separate identities per app.
- Implemented?: Partial. OIDC/workload identity exists, but roles include write-level access for runtime.
- Priority: P1

#### CC-4: Secrets management and no secrets in repo

- Why: Prevent leakage; SOC 2 confidentiality expectation.
- How: Store secrets in Key Vault; consume via CSI driver or pipeline variable references; rotate.
- Options:
  - Azure DevOps secret variables (current)
  - Key Vault + pipeline integration
  - Key Vault + CSI driver + managed identity
- Best option: Key Vault + CSI driver for workloads; pipelines read from Key Vault.
- Implemented?: No. Secrets are present in `terraform/azure/dev/terraform.tfvars`.
- Priority: P0

#### CC-5: Network segmentation and ingress hardening

- Why: Limit exposure and reduce attack surface.
- How: Restrict NSG rules, isolate private subnets, use WAF-based ingress, restrict AKS API server.
- Options:
  - Public NGINX ingress LB (current)
  - Application Gateway WAF + private backend
  - Front Door Premium WAF + private backend
- Best option: Front Door Premium WAF + private ingress + AKS API server authorized IPs.
- Implemented?: **No**. NSG for private subnets allows inbound 80/443 from the Internet and allows NodePort range 30000-32767; WAF not implemented.
- Priority: P0

#### CC-6: Private endpoints and disable public access

- Why: SOC 2 expects confidential data not exposed to public network.
- How: Private endpoints for Key Vault, Storage, ACR, Postgres, Redis; disable public access.
- Options:
  - Public access with firewall rules (current)
  - Private endpoints only
  - Private endpoints + service endpoints + firewall
- Best option: Private endpoints + public access disabled + private DNS zones.
- Implemented?: **Partial**.
  - Completed (dev, applied 2025-12-22):
    - Key Vault: public access disabled + Private Endpoints created + Private DNS zone (`privatelink.vaultcore.azure.net`) linked to the VNet.
    - Evidence: `terraform/azure/dev` now has `enable_key_vault_private_endpoints = true` and the Key Vault module enforces Private Link.
  - Remaining (not done yet): Storage, ACR, Postgres, Redis private endpoints + public access disable + Private DNS zones.
  - Tracking: see `docs/Development Documentation/private-access-implementation-tracker.md`.
- Priority: P0

#### CC-7: Secure defaults for data services

- Why: Reduce misconfiguration risk and meet security baseline.
- How:
  - Storage: HTTPS-only, min TLS 1.2, disable public access, soft delete, versioning
  - Postgres: require SSL, HA, backups, no public access
  - Redis: TLS 1.2, disable non-SSL, Premium tier
  - ACR: disable public access, enable content trust/scanning
- Options: Default vs hardened vs policy-enforced.
- Best option: Hardened config + Azure Policy enforcement.
- Implemented?: **No**. Secure-default hardening is not fully enforced via Terraform across all dev services (and is not policy-enforced).
- Priority: P0

#### CC-8: Centralized logging and monitoring

- Why: SOC 2 requires audit trails and incident detection.
- How: Log Analytics workspace + diagnostic settings for all Azure services + alert rules.
- Options:
  - Basic logs
  - Log Analytics + Azure Monitor alerts
  - Log Analytics + Sentinel (SIEM)
- Best option: Log Analytics + Azure Monitor alerts (Sentinel optional).
- Implemented?: No.
- Priority: P0

#### CC-9: Vulnerability management and image security

- Why: Prevent deployment of known-vulnerable images.
- How: Defender for Cloud for ACR and AKS, image scanning, admission policies.
- Options:
  - Manual scans
  - Defender for Cloud
  - Defender + signed images (cosign) + policy enforcement
- Best option: Defender + signed images + Azure Policy for Kubernetes.
- Implemented?: No.
- Priority: P1

#### CC-10: Terraform state protection

- Why: Terraform state can contain secrets and infrastructure metadata.
- How: Dedicated storage account with RBAC, private endpoint, versioning, soft delete, and limited access.
- Options:
  - Local state (avoid)
  - Remote state (current)
  - Remote state + private endpoint + policy
- Best option: Remote state with private endpoint, RBAC, soft delete, and versioning.
- Implemented?: Partial. Remote state exists but hardening is not defined.
- Priority: P0

### Availability (A)

#### A-1: AKS high availability and autoscaling

- Why: Ensure compute resiliency and scale under load.
- How: Multiple node pools across availability zones, cluster autoscaler enabled.
- Options:
  - Single pool (current)
  - Multi-zone pool
  - Multi-zone + autoscaler
- Best option: Multi-zone + autoscaler + separate system/user pools.
- Implemented?: Partial. No availability zones or autoscaler configured.
- Priority: P0

#### A-2: Ingress resilience and DDoS protection

- Why: Protect public endpoints and ensure uptime.
- How: WAF in front of ingress, DDoS Standard for VNet, health probes and autoscale.
- Options:
  - NGINX public LB (current)
  - App Gateway WAF
  - Front Door Premium WAF + App Gateway
- Best option: Front Door Premium WAF + App Gateway or private NGINX ingress.
- Implemented?: Partial. NGINX with two replicas; no WAF/DDoS.
- Priority: P0

#### A-3: Database HA, backups, and geo-redundancy

- Why: Meet RTO/RPO commitments.
- How: Postgres Flexible Server HA, zone redundant, backup retention 7-35 days, geo-backups.
- Options:
  - Single server (current)
  - Zone redundant HA
  - HA + geo-redundant backups
- Best option: HA + geo-redundant backups.
- Implemented?: No. Public access enabled; backups set to 7 days; no HA.
- Priority: P0

#### A-4: Redis high availability

- Why: Avoid cache downtime and data loss.
- How: Premium tier with replicas, persistence, and zone redundancy.
- Options:
  - Basic (current)
  - Standard
  - Premium
- Best option: Premium with replicas and persistence.
- Implemented?: No.
- Priority: P1

#### A-5: Storage durability

- Why: Protect customer data.
- How: Use ZRS/GZRS or GRS; enable versioning and soft delete.
- Options:
  - LRS (current)
  - ZRS
  - GRS/GZRS
- Best option: GZRS (or GRS) for production.
- Implemented?: No. LRS is used.
- Priority: P1

#### A-6: App Service availability

- Why: Frontend uptime requirements.
- How: Premium v3 plan, autoscale, zone redundancy, health checks.
- Options:
  - Basic (current)
  - Standard
  - Premium v3
- Best option: Premium v3 with autoscale.
- Implemented?: No. B1/B2 plans.
- Priority: P1

#### A-7: DR plan and testing

- Why: SOC 2 Availability expects documented DR and tests.
- How: Define RTO/RPO, runbooks, and perform DR tests at least annually.
- Options:
  - Ad-hoc
  - Documented plan
  - Documented plan + scheduled tests
- Best option: Documented plan + annual test + evidence.
- Implemented?: No (process work).
- Priority: P2

### Processing Integrity (PI)

#### PI-1: Change management and release approvals

- Why: Ensure changes are authorized and tested.
- How: Branch policies, required reviews, CI checks, deployment approvals.
- Options:
  - Direct merges
  - PR reviews
  - PR reviews + release gates
- Best option: PR reviews + pipeline environment approvals.
- Implemented?: Partial. Pipelines exist but no approval gates are defined in Terraform.
- Priority: P1

#### PI-2: Infrastructure drift detection

- Why: Detect unauthorized or accidental changes.
- How: Scheduled Terraform plan and alert on drift.
- Options:
  - Manual checks
  - Scheduled plan
  - Scheduled plan + policy alerts
- Best option: Scheduled plan + alerts.
- Implemented?: No.
- Priority: P2

#### PI-3: Runtime configuration integrity

- Why: Prevent tampering with app settings/secrets.
- How: Store runtime config in Key Vault or App Configuration; lock down AKS namespaces.
- Options:
  - Environment variables
  - Key Vault references
  - Key Vault + signed images + RBAC
- Best option: Key Vault references + signed images + strict RBAC.
- Implemented?: Partial. App Service uses app settings; AKS uses workload identity but not full config hardening.
- Priority: P1

### Confidentiality (C)

#### C-1: Encryption in transit enforcement

- Why: Protect data over the network.
- How: Enforce TLS 1.2+ for Storage, Redis, Postgres; require SSL for DB connections.
- Options:
  - Default
  - TLS enforcement
  - TLS enforcement + CMK for sensitive data
- Best option: TLS enforcement + CMK if required by customer contracts.
- Implemented?: **Partial**. App Services are typically HTTPS-only, but consistent TLS 1.2+ enforcement across Storage/PostgreSQL/Redis is not confirmed in the current dev Terraform.
- Priority: P0

#### C-2: Key Vault network isolation

- Why: Restrict secret access to private networks.
- How: Private endpoint + firewall allowlist + disable public access.
- Options:
  - Public access (current)
  - Private endpoint
  - Private endpoint + firewall
- Best option: Private endpoint + firewall.
- Implemented?: **Partial**.
  - Completed (dev, applied 2025-12-22): Key Vault public network access disabled and Private Endpoints enabled.
  - Remaining: add explicit Key Vault network ACL hardening (deny-by-default / bypass rules) as a separate step if desired.
- Priority: P0

#### C-3: Data access audit logging

- Why: SOC 2 requires evidence of access control effectiveness.
- How: Diagnostic settings for Key Vault, Storage, Postgres, and ACR to Log Analytics.
- Options:
  - No logs
  - Diagnostic logs
  - Diagnostic logs + alerting
- Best option: Diagnostic logs + alerting.
- Implemented?: No.
- Priority: P0

### Privacy (P)

#### P-1: Data retention and deletion

- Why: Privacy commitments require retention limits and deletion support.
- How: Storage lifecycle policies, DB retention policy, documented deletion workflows.
- Options:
  - Manual deletion
  - Lifecycle policies
  - Automated lifecycle + documented process
- Best option: Automated lifecycle + documented process.
- Implemented?: No.
- Priority: P2

#### P-2: Audit trail for personal data access

- Why: Provide evidence for data access and deletion requests.
- How: Centralized logging with queries and audit trails.
- Options:
  - Manual logging
  - Centralized logging
  - Centralized logging + audit dashboards
- Best option: Centralized logging + dashboards.
- Implemented?: No.
- Priority: P2

## Recommended implementation roadmap

Phase 0 (P0, audit blockers):

- Remove secrets from `terraform.tfvars`; move to Key Vault and rotate.
- Disable public access + add private endpoints for Key Vault (DONE in dev, 2025-12-22), Storage (TODO), ACR (TODO), Postgres (TODO), Redis (TODO).
- Harden Postgres (no public access, firewall restricted, SSL required).
- Create Log Analytics + diagnostics for all services.
- Replace subscription Contributor for CI/CD with least-privilege roles.
- Lock down NSG rules; remove NodePort and public inbound to private subnets.

Phase 1 (P1, production readiness):

- AKS multi-zone + autoscaler; separate system/user pools.
- WAF and DDoS protection (Front Door Premium or App Gateway WAF).
- App Service and Redis Premium tiers for HA.
- Storage replication upgrade (GRS/GZRS) + soft delete/versioning.
- Defender for Cloud + image signing + policy enforcement.

Phase 2 (P2, maturity):

- Formal DR plan with test evidence.
- Drift detection and continuous compliance checks.
- Privacy retention automation and audit dashboards.

## Notes for implementation

- Production should live in a separate `terraform/azure/prod` folder with distinct state and access controls.
- If `terraform/azure/dev/terraform.tfvars` was ever committed, rotate all credentials immediately.
- Keep evidence: logging configs, role assignments, backup settings, policy definitions, and approval workflows.
- To enable private endpoints in production, set `enable_private_endpoints = true` in terraform.tfvars.
- See [Private Endpoints Implementation Guide](private-endpoints-implementation.md) for detailed configuration.

**Last Updated**: 2025-12-22
