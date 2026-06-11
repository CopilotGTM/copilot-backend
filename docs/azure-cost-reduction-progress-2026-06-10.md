# Azure Cost Reduction Progress - 2026-06-10

This document records what has been investigated and changed so far in the Azure cost-reduction work. It should be read together with `docs/azure-cost-analysis-2026-05.md`, which contains the billing-export breakdown.

## Source Data

- Billing export: `C:\Users\nikhi\OneDrive\Desktop\AzureUsage (4).csv`
- Billing period in export: 2026-05-01 through 2026-06-06
- Export total: `$3,688.87`
- Analysis date: 2026-06-10
- Subscription checked live: `Microsoft Azure Sponsorship`

## What We Found

The last-month export was dominated by infrastructure, not local development.

| Rank | Area | Cost | Finding |
|---:|---|---:|---|
| 1 | NAT Gateway | `$1,408.24` | Largest cost bucket and the only major spike. |
| 2 | Virtual Machines | `$799.90` | Mostly steady AKS/VM compute cost. |
| 3 | Application Gateway | `$490.43` | Mostly fixed Standard v2 cost. |
| 4 | PostgreSQL | `$390.05` | Mostly General Purpose Dsv3 compute. |
| 5 | Storage | `$221.49` | Mostly managed disk cost. |
| 6 | App Service | `$108.75` | Basic Linux B1/B2 plans. |
| 7 | Foundry Models / Azure OpenAI | `$98.54` | Meaningful, but not the main cost driver. |

The cost jump started around 2026-05-26. The daily bill moved from roughly `$65-$80/day` to about `$165/day`, and the difference was almost entirely NAT Gateway data processing.

## NAT Gateway Investigation

Two NAT Gateways were found:

| Environment | NAT Gateway | Resource group |
|---|---|---|
| Dev | `copilot-gtm-vnet-dev-nat-gw` | `copilot-gtm-app-dev` |
| Prod | `copilot-gtm-vnet-prod-nat-gw` | `copilot-gtm-app-prod` |

The high traffic mapped to prod, not dev.

From 2026-05-26 to 2026-06-10:

| Environment | NAT In | NAT Out | Notes |
|---|---:|---:|---|
| Prod | `27.10 TB` | `361.59 GB` | Spike source. |
| Dev | `294.86 GB` | `108.12 GB` | Much lower. |

Bad prod dates included:

| Date | Approx prod NAT data |
|---|---:|
| 2026-05-26 | `2.97 TB` |
| 2026-05-27 | `3.29 TB` |
| 2026-05-28 | `3.16 TB` |
| 2026-05-29 | `2.91 TB` |
| 2026-05-30 | `3.06 TB` |
| 2026-05-31 | `3.01 TB` |
| 2026-06-01 | `3.02 TB` |
| 2026-06-02 | `3.00 TB` |
| 2026-06-03 | `1.92 TB` |

After that, prod NAT traffic dropped sharply. On 2026-06-10, prod was around `29.52 GB` total and dev was around `19.03 GB` total for the checked window.

Important interpretation:

- The spike was mostly `In` direction on the NAT metric, not outbound upload volume.
- Packet drops were `0` for the checked period.
- This points more toward internal workloads downloading large response payloads through NAT than a public inbound attack.
- There were no NAT diagnostic settings or flow logs available, so exact pod/source attribution was not possible after the fact.

## Prod Deployment Pool Finding

Around the start of the spike, the activity log showed changes involving the prod Managed DevOps pool and prod AKS GPU pool.

Observed caller:

- Service principal display name: `copilot-gtm-devops-sp-prod`
- App ID: `353632e1-c40b-498c-83ed-52accf6a60d8`

Prod DevOps pool:

- Name: `copilot-devops-prod-deploy`
- Resource group: `copilot-gtm-app-prod`
- Agent profile: `Stateless`
- Maximum concurrency: `2`
- Parallelism: `2`
- Provisioning state: `Failed`
- Subnet: `copilot-gtm-vnet-prod-mdp-prod-deploy`
- Last modified: `2026-05-26T03:51:54Z`

Dev DevOps pool differed materially:

- Name: `copilot-devops-dev-deploy`
- Agent profile: `Stateful`
- Maximum concurrency: `1`
- Provisioning state: `Succeeded`

Lesson: deployment alone was not enough to explain the spike because dev deployments did not produce similar traffic. The prod deployment infrastructure had different behavior and state.

## NAT Gateway Lessons

1. NAT Gateway is not mandatory for every subnet, but some egress path is required if private workloads must reach the public internet.
2. Replacing NAT Gateway does not create free internet egress. Alternatives move the cost and operational burden.
3. Reasonable alternatives include AKS Standard Load Balancer outbound, Private Endpoints, Service Endpoints, Microsoft-hosted agents, self-managed NAT/proxy VM, or Azure Firewall.
4. Azure Firewall is usually more expensive than NAT Gateway, but gives better logging and control.
5. A self-managed NAT/proxy VM can be cheaper at low traffic, but it adds patching, availability, scaling, and failure-mode ownership.
6. Private Endpoints and Service Endpoints are the best way to avoid NAT for Azure-to-Azure traffic.
7. Future spikes need flow logs or diagnostic logging enabled before the incident, otherwise attribution is limited to subnet/resource-level inference.

## PostgreSQL Dev Cost Reduction

Dev PostgreSQL was changed from General Purpose to Burstable.

Terraform source changed in `infra/terraform/azure/dev/terraform.tfvars`:

| Setting | Before | After |
|---|---|---|
| `db_sku_name` | `GP_Standard_D2s_v3` | `B_Standard_B1ms` |
| `pgbouncer_enabled` | `true` | `false` |
| PgBouncer pool sizing | configured | removed from dev tfvars |

Reason:

- `B_Standard_B1ms` is much cheaper and appropriate for dev.
- PgBouncer requires General Purpose or higher, so it cannot remain enabled on the Burstable dev tier.
- With PgBouncer disabled, the application must connect directly to PostgreSQL on port `5432` instead of PgBouncer port `6432`.

Current retail compute estimates checked against Azure pricing:

| SKU | Tier | vCores | Approx compute/month |
|---|---|---:|---:|
| `B_Standard_B1ms` | Burstable | 1 | `$14/month` |
| `GP_Standard_D2s_v3` | General Purpose | 2 | `$141/month` |

Expected compute saving for dev PostgreSQL is roughly `$127/month`, before storage and backup charges.

## Live Dev PostgreSQL Changes Applied

Terraform plan was inspected, but not applied because the broader plan included unrelated drift/provider issues outside this cost-reduction change.

Instead, the live dev change was applied directly with Azure CLI:

- Disabled PgBouncer on the dev PostgreSQL server.
- Updated the dev PostgreSQL SKU to `Standard_B1ms` on the Burstable tier.
- Verified the server returned to `Ready` state.
- Verified `pgbouncer.enabled` became unsupported afterward, which is expected on Burstable.

## Backend Port Updates Applied

Because PgBouncer was disabled, the backend had to use port `5432`.

Updates applied:

- Azure DevOps variable group `DATABASE_PORT` changed from `6432` to `5432`.
- Dev Kubernetes secret `copilot-backend-secret` updated so `DATABASE_PORT=5432`.
- Active dev deployments restarted so they picked up the new secret.

Verified:

- Dev context was selected before Kubernetes mutation.
- Active dev deployments reached `1/1` readiness.
- `kubectl exec deploy/copilot-backend -n copilotgtm -- printenv DATABASE_PORT` returned `5432`.
- Recent API logs showed startup complete and webhook requests returning HTTP 202.

YAML check:

- Backend pipeline YAML already reads `$(DATABASE_PORT)`.
- Helm values did not hardcode `6432` or `5432` for this setting.
- No backend YAML edit was required for the port change.

## Worktree Isolation

The Azure cost changes were moved out of the original checkout and into a sibling worktree.

New worktree:

- Path: `D:\CopilotGTM\copilot-backend-azure-cost-reduction`
- Branch: `codex/azure-cost-reduction-dev-db`

Moved files:

- `infra/terraform/azure/dev/terraform.tfvars`
- `docs/azure-cost-analysis-2026-05.md`
- `docs/azure-cost-reduction-progress-2026-06-10.md`

The original checkout was cleaned of the Azure-cost Terraform/doc changes. Unrelated dirty files in the original checkout were left untouched.

## What Is Implemented So Far

Implemented:

- CSV cost analysis document.
- Live NAT Gateway attribution and daily traffic summary.
- Dev PostgreSQL downgrade to Burstable B1ms.
- Dev PgBouncer disabled.
- Dev backend `DATABASE_PORT` moved to `5432` in Azure DevOps variable group and live Kubernetes secret.
- Dev deployments restarted and verified after the DB port change.
- Terraform source updated to match the intended dev PostgreSQL state.
- Work moved into an isolated sibling worktree.

Not implemented yet:

- NAT Gateway replacement/removal.
- Flow logs or NAT diagnostics.
- Dev AKS node count reduction.
- Dev Application Gateway removal/replacement.
- Storage cleanup for unattached disks/snapshots/images.
- Prod PostgreSQL right-sizing.

## Recommended Next Steps

1. Enable network attribution before making more NAT decisions.
   - Enable NSG flow logs or equivalent diagnostic logging for prod subnets that route through NAT.
   - Add alerts for NAT data processed spikes.

2. Reduce dev AKS cost.
   - Check current dev node pools.
   - Reduce dev minimum node count if workload availability allows it.
   - Scale unused dev workers to zero.

3. Reduce dev Application Gateway cost.
   - Confirm whether dev needs a dedicated Application Gateway full time.
   - Consider a cheaper dev ingress path if production parity is not required 24/7.

4. Clean storage.
   - Delete unattached dev disk(s) after confirming they are not needed.
   - Review old snapshots, stale persistent volumes, old ACR images, and backup retention.

5. Keep prod PostgreSQL metrics-led.
   - Do not move prod blindly to Burstable.
   - Check CPU, memory, IOPS, connections, latency, and slow queries before changing prod DB SKU.

## Key Lessons

- Billing CSVs without resource IDs, resource groups, or tags are not enough for clean dev/prod attribution.
- NAT data processing can dominate the bill quickly; monitoring must be in place before the next spike.
- A traffic spike being mostly NAT `In` does not mean internet users pushed 3 TB into the system. It can mean private workloads downloaded large volumes through NAT.
- Dev and prod can have very different behavior even when both run deployments; deployment infrastructure state and agent profile matter.
- PgBouncer availability depends on PostgreSQL tier. Downgrading dev to Burstable requires direct DB connections on port `5432`.
- Pipeline YAML can be correct while runtime Kubernetes secrets are stale. Both source-of-truth and live runtime state need verification.
- Terraform plan must be treated carefully in a drifted environment. If unrelated replacements appear, do not apply a broad plan just to make one cost change.
- Isolating cost-reduction work in a sibling worktree keeps it separate from unrelated backend changes.
