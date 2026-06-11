# Azure Cost Analysis - May 2026 Billing Export

Source file: `C:\Users\nikhi\OneDrive\Desktop\AzureUsage (4).csv`

Analysis date: 2026-06-10

## Executive Summary

The export contains 1,855 billing rows for the `Microsoft Azure Sponsorship`
subscription from 2026-05-01 through 2026-06-06. Total cost in the file is
`$3,688.87`.

The main cost driver is not local development. The CSV shows cloud infrastructure
costs, led by NAT Gateway, virtual machines, Application Gateway, PostgreSQL, and
managed disks.

Top-level spend:

| Rank | Service | Cost | Share |
|---:|---|---:|---:|
| 1 | NAT Gateway | `$1,408.24` | 38.2% |
| 2 | Virtual Machines | `$799.90` | 21.7% |
| 3 | Application Gateway | `$490.43` | 13.3% |
| 4 | Azure Database for PostgreSQL | `$390.05` | 10.6% |
| 5 | Storage | `$221.49` | 6.0% |
| 6 | Azure App Service | `$108.75` | 2.9% |
| 7 | Foundry Models / Azure OpenAI | `$98.54` | 2.7% |
| 8 | Load Balancer | `$43.90` | 1.2% |
| 9 | Container Registry | `$43.59` | 1.2% |
| 10 | Redis Cache | `$38.59` | 1.0% |

## Important Limitation

This CSV has service, meter, region, quantity, and cost. It does not include
resource group, resource name, tags, or resource ID. Because of that, it cannot
prove which exact costs are dev versus prod by itself.

To split dev and prod confidently, the next export should include at least one
of:

- Resource ID
- Resource group
- Tags such as `env=dev` and `env=prod`
- Azure Cost Management grouping by resource

Until then, this document identifies the cost buckets and the highest-probability
reduction targets.

## Daily Cost Pattern

Spend was around `$65-$80/day` through 2026-05-25, then jumped sharply:

| Period | Days | Total | Daily Average |
|---|---:|---:|---:|
| 2026-05-01 to 2026-05-25 | 25 | `$1,702.93` | `$68.12/day` |
| 2026-05-26 to 2026-06-06 | 12 | `$1,985.94` | `$165.49/day` |

The jump is almost entirely NAT Gateway data processing.

| Service | Before 2026-05-26 | From 2026-05-26 | Daily Delta |
|---|---:|---:|---:|
| NAT Gateway | `$6.04/day` | `$104.78/day` | `+$98.74/day` |
| Foundry Models / Azure OpenAI | `$2.32/day` | `$3.38/day` | `+$1.07/day` |
| Storage | `$5.90/day` | `$6.17/day` | `+$0.28/day` |
| Virtual Machines | `$21.74/day` | `$21.37/day` | `-$0.37/day` |
| PostgreSQL | `$10.67/day` | `$10.27/day` | `-$0.40/day` |
| Application Gateway | `$13.82/day` | `$12.07/day` | `-$1.76/day` |

NAT Gateway data processed moved from roughly `40-125` units/day earlier in May
to roughly `2,800-3,300` units/day from 2026-05-26 through 2026-06-02.

## Top Meter-Level Costs

| Meter | Cost | Share | Notes |
|---|---:|---:|---|
| NAT Gateway - Standard Data Processed | `$1,329.22` | 36.0% | Primary cost spike. Investigate outbound traffic path immediately. |
| Virtual Machines - D2as v6 | `$743.88` | 20.2% | Steady compute cost, likely AKS node pool or VM workload. |
| Application Gateway - Standard Fixed Cost | `$431.98` | 11.7% | Fixed daily cost; savings require SKU/topology change or removal. |
| PostgreSQL Flexible Server GP Dsv3 Compute - vCore | `$338.93` | 9.2% | Database compute cost; inspect dev/prod sizing and uptime. |
| Premium SSD Managed Disks - P10 LRS Disk | `$204.82` | 5.6% | Attached disk cost; likely VM/AKS node disks or persistent volumes. |
| NAT Gateway - Standard Gateway | `$79.02` | 2.1% | Fixed NAT Gateway hourly charge. |
| App Service Basic Linux B2 | `$61.39` | 1.7% | App Service plan cost. |
| Application Gateway - Standard Capacity Units | `$58.46` | 1.6% | Variable App Gateway capacity cost. |
| App Service Basic Linux B1 | `$47.36` | 1.3% | App Service plan cost. |
| Load Balancer rules | `$43.90` | 1.2% | Fixed load balancer/rules cost. |

## Region Breakdown

| Region | Cost | Share |
|---|---:|---:|
| US Central | `$1,975.72` | 53.6% |
| Global | `$1,452.14` | 39.4% |
| All | `$130.79` | 3.5% |
| US East 2 | `$98.54` | 2.7% |
| IN Central | `$31.41` | 0.9% |

`Global` is mostly NAT Gateway and Load Balancer billing. `US Central` carries
most VM, Application Gateway, PostgreSQL, and disk cost.

## What We Are Paying For

### 1. NAT Gateway

Cost: `$1,408.24`

This is the largest cost bucket and the only major spike. The data processing
meter alone is `$1,329.22`.

Likely causes to verify:

- A workload started sending high-volume outbound traffic through NAT Gateway
  around 2026-05-26.
- AKS pods, workers, or batch jobs began moving large payloads to external
  services.
- Container image pulls, storage transfers, backups, webhook replay, log export,
  or integration sync traffic increased.
- Dev and prod may be sharing a NAT design where dev traffic is not isolated or
  not capped.

Reduction options:

- Identify the subnet attached to the NAT Gateway and map which AKS node pools,
  App Services, or private resources use it.
- Check network metrics for bytes processed by NAT Gateway from 2026-05-26 to
  2026-06-02.
- Route Azure-to-Azure traffic through private endpoints or service endpoints
  where possible so it does not leave through NAT.
- Stop or scale down dev workloads that produce outbound traffic.
- Split dev/prod NAT visibility with tags, separate NAT Gateways, or resource
  group grouping so a spike is attributable next time.

### 2. Virtual Machines

Cost: `$799.90`

Main VM meter is D2as v6 at `$743.88`, with smaller D2a v4/D2as v4 and D2s v5
charges.

Likely source:

- AKS node pools or standalone VMs in US Central.

Reduction options:

- Confirm node pools and VM scale sets in dev/prod.
- For dev, scale AKS node pools to the minimum viable size outside working
  hours.
- Remove old node pools after migrations.
- Use smaller burstable/dev SKUs where acceptable.
- Right-size prod based on actual CPU and memory usage, not requested capacity.

### 3. Application Gateway

Cost: `$490.43`

Most of this is fixed Standard v2 cost, not traffic-sensitive usage.

Reduction options:

- Confirm whether both dev and prod need separate Application Gateways.
- For dev, consider a cheaper ingress path if production parity is not required
  full time.
- Remove orphaned public IP, listener, or gateway resources after local/dev
  cleanup.
- Keep prod gateway if it is the required public ingress and WAF/routing surface.

### 4. PostgreSQL Flexible Server

Cost: `$390.05`

Most spend is US Central General Purpose Dsv3 compute at `$338.93`. IN Central
burstable B1ms is small at `$21.49`, and storage is about `$29.64`.

Reduction options:

- Confirm which server is dev and which is prod.
- For dev, use burstable compute, stop when not needed if operationally safe, or
  lower vCore count.
- Review storage growth and backup retention separately.
- Avoid down-sizing prod without checking CPU, memory, IOPS, connection count,
  and slow query pressure.

### 5. Storage / Managed Disks

Cost: `$221.49`

Main meter is Premium SSD Managed Disk P10 LRS at `$204.82`.

Reduction options:

- Find unattached disks and old snapshots.
- Check AKS persistent volumes that are no longer bound.
- Move non-prod disks from Premium SSD to Standard SSD where latency is not
  important.
- Delete stale build artifacts, backups, and container layers after retention is
  agreed.

### 6. App Service

Cost: `$108.75`

Basic Linux B2 and B1 plans are visible.

Reduction options:

- Confirm whether these are still needed after local cleanup.
- Consolidate dev apps onto fewer plans where isolation is not required.
- Scale dev plans down or stop unused apps.

### 7. Foundry Models / Azure OpenAI

Cost: `$98.54`

This is meaningful but not the main problem. It includes GPT-5 and Azure OpenAI
media meters in US East 2.

Reduction options:

- Add per-environment and per-agent usage logging.
- Cap dev model usage.
- Route low-risk dev/test flows to cheaper models.
- Cache repeated prompt results where safe.
- Keep production model reductions tied to quality checks.

## Priority Reduction Plan

### Priority 1 - Investigate NAT Gateway Spike

This is the fastest path to real savings.

Actions:

1. In Azure, open NAT Gateway metrics for 2026-05-26 through 2026-06-02.
2. Identify attached subnets and resources.
3. Cross-check AKS, workers, image pulls, integration syncs, blob transfers, and
   external API calls around the spike window.
4. Decide whether the traffic is legitimate prod traffic, runaway dev traffic,
   or misrouted Azure-to-Azure traffic.

Expected impact:

- If NAT returns to pre-spike levels, daily cost drops by about `$98/day`.
- Monthlyized impact is roughly `$2,900/month`.

### Priority 2 - Separate Dev and Prod Cost Attribution

Actions:

1. Export Cost Management grouped by resource group or resource ID.
2. Add or enforce tags: `env`, `service`, `owner`, `cost_center`.
3. Create a daily budget alert for dev.
4. Create a separate alert for NAT Gateway data processed.

Expected impact:

- Prevents guessing. Makes future cost reviews directly actionable.

### Priority 3 - Right-Size Always-On Dev Infrastructure

Targets:

- AKS node pools / VMs
- Application Gateway
- PostgreSQL
- App Service plans
- Redis

Actions:

1. List dev resources and current SKUs.
2. Check 7-30 day CPU, memory, IOPS, request, and connection metrics.
3. Scale down dev first; keep prod changes evidence-based.
4. Delete orphaned resources from prior local/dev cleanup.

Expected impact:

- Base cost before the NAT spike was still about `$68/day`, or about
  `$2,040/month`. Most of that is fixed infrastructure.

## Next Data Needed

To move from analysis to cleanup decisions, collect:

- Azure Cost Management export grouped by `ResourceId`
- Resource list with `name`, `resourceGroup`, `type`, `location`, `sku`, `tags`
- NAT Gateway attached subnets and byte metrics
- AKS node pools, VMSS SKUs, min/max/current node counts
- PostgreSQL servers, SKUs, storage, backup retention, high availability setting
- Application Gateway SKU, instance/capacity config, listeners, public IPs
- Managed disks with attached/unattached status

## Working Conclusion

The current spend problem is not caused by local resources after local cleanup.
It is cloud-side, and the immediate hotspot is NAT Gateway data processing after
2026-05-26. The next step should be a live Azure inventory and NAT Gateway metric
trace so we can mark each cost as dev, prod, shared, or orphaned before changing
infrastructure.

## NAT Gateway Live Attribution - 2026-06-10

Azure currently has two NAT Gateways in the subscription:

| Environment | NAT Gateway | Resource Group | Location |
|---|---|---|---|
| Dev | `copilot-gtm-vnet-dev-nat-gw` | `copilot-gtm-app-dev` | `centralus` |
| Prod | `copilot-gtm-vnet-prod-nat-gw` | `copilot-gtm-app-prod` | `centralus` |

The large billing spike maps to the prod NAT Gateway, not dev.

Daily `ByteCount` from Azure Monitor:

| Date | Dev NAT Bytes | Prod NAT Bytes | Attribution |
|---|---:|---:|---|
| 2026-05-24 | 19.8 GB | 93.7 GB | Normal baseline |
| 2026-05-25 | 28.9 GB | 106.2 GB | Normal baseline |
| 2026-05-26 | 31.9 GB | 3.19 TB | Prod spike starts |
| 2026-05-27 | 26.8 GB | 3.53 TB | Prod spike |
| 2026-05-28 | 17.4 GB | 3.39 TB | Prod spike |
| 2026-05-29 | 18.1 GB | 3.13 TB | Prod spike |
| 2026-05-30 | 23.0 GB | 3.29 TB | Prod spike |
| 2026-05-31 | 25.0 GB | 3.23 TB | Prod spike |
| 2026-06-01 | 27.5 GB | 3.24 TB | Prod spike |
| 2026-06-02 | 24.2 GB | 3.22 TB | Prod spike |
| 2026-06-03 | 27.0 GB | 2.06 TB | Prod spike decreasing |
| 2026-06-04 | 30.6 GB | 381.9 GB | Spike falling |
| 2026-06-05 | 25.3 GB | 272.7 GB | Spike falling |
| 2026-06-06 | 31.7 GB | 320.4 GB | Spike falling |
| 2026-06-07 | 45.1 GB | 83.7 GB | Near baseline |
| 2026-06-08 | 23.5 GB | 131.1 GB | Elevated but much lower |
| 2026-06-09 | 35.2 GB | 7.2 GB | Low |
| 2026-06-10 | 17.1 GB | 28.3 GB | Low partial day |

The prod direction split shows the spike was mostly `In` traffic:

| Date | Prod NAT In | Prod NAT Out |
|---|---:|---:|
| 2026-05-26 | 3.16 TB | 29.4 GB |
| 2026-05-27 | 3.50 TB | 24.1 GB |
| 2026-05-28 | 3.37 TB | 13.4 GB |
| 2026-05-29 | 3.11 TB | 13.0 GB |
| 2026-05-30 | 3.27 TB | 13.8 GB |
| 2026-05-31 | 3.21 TB | 13.6 GB |
| 2026-06-01 | 3.23 TB | 13.1 GB |
| 2026-06-02 | 3.21 TB | 10.3 GB |
| 2026-06-03 | 2.04 TB | 22.9 GB |
| 2026-06-04 | 332.7 GB | 49.2 GB |
| 2026-06-05 | 216.1 GB | 56.6 GB |
| 2026-06-06 | 252.7 GB | 67.7 GB |

Interpretation:

- This looks like prod workloads downloading large response payloads through NAT,
  not serving public inbound user traffic.
- The first hourly jump happened on 2026-05-26 between `03:00` and `04:00` UTC.
- The prod managed DevOps pool was modified at `2026-05-26T03:51:54Z`, and it
  sits in a NAT-attached subnet.
- Several prod backend/infra/admin pipeline runs also happened in the same
  `02:46-03:52` UTC window.
- The prod NAT Gateway also serves the AKS private subnet, so AKS workloads are
  still a candidate source. Current evidence narrows the source to prod AKS
  and/or the prod managed DevOps deploy pool.

Prod NAT-attached subnets:

| Subnet | Purpose |
|---|---|
| `copilot-gtm-vnet-prod-private-1` | AKS prod node pools |
| `copilot-gtm-vnet-prod-private-2` | AKS/private workload subnet |
| `copilot-gtm-vnet-prod-mdp-prod-deploy` | Managed DevOps prod deploy pool |

No diagnostic settings or Network Watcher flow logs are currently enabled for
the prod NAT path, so Azure does not have destination/source flow attribution for
the spike window.

Immediate reduction plan:

1. Keep the prod managed DevOps pool under review first because its modification
   timestamp aligns exactly with the spike start and it uses the NAT-attached
   `mdp-prod-deploy` subnet.
2. Confirm whether the prod deploy pool still needs NAT, stateless agents, and
   `maximumConcurrency=2`; lower or disable the pool when not actively deploying
   if product operations allow it.
3. Enable NSG flow logs or equivalent network flow telemetry on the prod
   NAT-attached subnets before the next deploy window, so future spikes identify
   source IP and destination.
4. Check prod pipeline steps for repeated large downloads: Docker base image
   pulls, ACR pulls, `uv sync`, `npm/yarn install`, Terraform provider downloads,
   Helm/chart pulls, Azure CLI image/tool bootstrap, and artifact downloads.
5. Check AKS pods/jobs for repeated large external pulls or sync jobs around
   2026-05-26 to 2026-06-03.
6. Add an Azure Monitor alert on prod NAT Gateway `ByteCount` so a TB/day spike
   is caught the same day, not at billing review.
