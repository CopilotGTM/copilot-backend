# Application Chart & Workers

This Helm umbrella chart deploys:

* API (FastAPI webservice)
* Celery workers (knowledge-extract, knowledge-upload, organization, chat, integrations)
* KEDA ScaledObjects for queue length-based autoscaling (Redis)
* A startup Job (migrations + seeding)
* GPU-enabled workers for image summarization and document processing

## Logging & Observability (AKS)

See [`docs/Technical Documentation/aks-logging-best-practices.md`](../docs/Technical%20Documentation/aks-logging-best-practices.md) for recommended AKS logging setup (Azure Monitor/Log Analytics) and KQL queries for both the API and Celery workers.

The chart enables the Flower UI (`flower.enabled=true`) and Celery events (`global.celery.enableEvents=true`) to monitor worker/task state across queues. Flower ingress remains disabled unless explicitly exposed.

## Worker Summary

| Worker                | Queue(s)                                     | Purpose                                                | Default Concurrency |
|-----------------------|----------------------------------------------|--------------------------------------------------------|---------------------|
| knowledge-extract     | `knowledge_extract`                          | GPU-intensive document parsing & embeddings            | 1                   |
| knowledge-upload      | `knowledge_upload`                           | Upload pipeline orchestration                          | 4                   |
| organization          | `organization`                               | Organisation provisioning & maintenance tasks          | 6                   |
| chat                  | `attachments`, `chat_processing`, `chat_messages`, `voice_realtime`, `chat_maintenance` | Chat ingestion, processing, messaging, and bounded maintenance | 10 threads          |
| integrations          | `integrations`, `integrations_priority`      | Third-party CRM/OAuth sync flows                       | 4                   |

The non-GPU workers now run higher concurrency because their tasks are short and I/O-bound; adjust CPU/memory requests in `charts/application/values.yaml` if you need to tune them further.

## GPU Node Pool Configuration

### Overview

A dedicated GPU node pool (`kgpu`) has been provisioned in the AKS cluster for GPU-intensive tasks like image summarization, embeddings generation, and document processing with visual elements.

### GPU Node Pool Specifications

| Property | Value | Description |
|----------|-------|-------------|
| Node Pool Name | `kgpu` | Dedicated GPU worker pool |
| VM Size | `Standard_NC4as_T4_v3` | NVIDIA Tesla T4 GPU |
| GPU Count per Node | 1 | One T4 GPU per node |
| vCPU | 4 | CPU cores per node |
| Memory | 28 GB RAM | Memory per node |
| Initial Node Count | 0 | **Scale-to-zero enabled** |
| Kubernetes Version | 1.32 | Matches cluster version |
| Mode | User | Allows scaling to zero |

### Node Isolation

The GPU node pool uses **taints and labels** to ensure only GPU workloads are scheduled:

**Node Labels:**

```yaml
workload: knowledge-gpu
```

**Node Taints:**

```yaml
dedicated=gpu:NoSchedule
```

**What this means:**

* ✅ Only pods with matching `nodeSelector` can target these nodes
* ✅ Only pods with proper `tolerations` can be scheduled
* ❌ Non-GPU workloads are automatically blocked from using expensive GPU nodes

### Knowledge Extract Worker GPU Configuration

The `knowledge-extract-worker` is configured with GPU support in `application/values.yaml`:

```yaml
knowledge-extract-worker:
  enabled: true
  replicaCount: 0  # Start with 0 replicas - scale from zero
  
  # GPU Node Pool Configuration
  gpu:
    enabled: true
    nodeSelector:
      workload: knowledge-gpu
    tolerations:
    - key: dedicated
      operator: Equal
      value: gpu
      effect: NoSchedule
  
  # GPU Resource Requests and Limits
  resources:
    requests:
      nvidia.com/gpu: 1      # Request 1 Tesla T4 GPU
      cpu: "2000m"           # Reserve 2 vCPU (of 4 available)
      memory: "16Gi"         # Reserve 16GB RAM (of 28GB available)
    limits:
      nvidia.com/gpu: 1      # Limit to 1 GPU
      cpu: "3500m"           # Allow burst to 3.5 vCPU
      memory: "24Gi"         # Allow burst to 24GB RAM
  
  # KEDA Autoscaling Configuration
  autoscaling:
    enabled: true
    minReplicas: 0           # Scale to zero when idle (cost optimization)
    maxReplicas: 1           # Single GPU node limit
    queueLength: 1           # Scale up when 1+ tasks in queue
    scaleDownDelay: 600      # Wait 10 minutes before scaling down
    scaleUpDelay: 10         # Check queue every 10 seconds
  
  # Pod Disruption Budget
  pdb:
    enabled: true
    minAvailable: 0          # Allow zero pods (supports scale-to-zero)
```

### Auto-Scaling Behavior

**When idle (no tasks):**

* Node count = **0**
* No GPU nodes running

**When task arrives:**

1. Kubernetes detects pending pod needs GPU
2. AKS Cluster Autoscaler provisions a new GPU node
3. Takes **3-5 minutes** for node to become ready
4. Pod schedules and starts processing

**When task completes:**

1. Pod terminates
2. After ~10 minutes of idle time, node is automatically deprovisioned
3. Returns to 0 nodes

### Verify GPU Worker Deployment

```powershell
# Check if pod is pending/running
kubectl get pods -n copilotgtm -l app.kubernetes.io/name=knowledge-extract-worker

# Check GPU node was provisioned (if pod is pending)
kubectl get nodes -l workload=knowledge-gpu

# Verify pod is on GPU node
kubectl get pod <pod-name> -n copilotgtm -o wide

# Check GPU allocation on node
kubectl describe node <node-name> | Select-String -Pattern "nvidia.com/gpu" -Context 0,5

# View pod events (troubleshooting)
kubectl describe pod <pod-name> -n copilotgtm

# Check NVIDIA device plugin is running
kubectl get pods -n kube-system | Select-String -Pattern "nvidia"
```

### GPU Troubleshooting

#### Pod Stuck in Pending State

**Cause:** Missing tolerations or nodeSelector mismatch

**Solution:** Verify all three GPU configurations are present:

1. ✅ `gpu.nodeSelector: {workload: knowledge-gpu}`
2. ✅ `gpu.tolerations` with `dedicated=gpu:NoSchedule`
3. ✅ `resources.requests[nvidia.com/gpu]: 1`

#### Node Takes Too Long to Provision

**Cause:** Azure capacity issues or quota limits

**Solution:** Check Azure quota for `NCasT4_v3` series in `eastus2` region

```powershell
az vm list-usage --location eastus2 --query "[?contains(name.value, 'NCasT4')]" -o table
```

#### Pod Fails with "UnexpectedAdmissionError"

**Cause:** GPU device plugin not ready

**Solution:** Wait 1-2 minutes after node provisioning, then retry. The NVIDIA device plugin is automatically installed by AKS.

#### GPU Not Detected in Container

**Cause:** GPU resource not requested or NVIDIA drivers not available

**Solution:**

```powershell
# Verify GPU is requested in pod spec
kubectl get pod <pod-name> -n copilotgtm -o yaml | Select-String -Pattern "nvidia.com/gpu"

# Check if NVIDIA device plugin exposed GPUs on node
kubectl describe node <node-name> | Select-String -Pattern "nvidia.com/gpu"

# Test GPU access inside pod
kubectl exec -it <pod-name> -n copilotgtm -- nvidia-smi
```

### Important Notes

#### NVIDIA Device Plugin

* ✅ Automatically installed by AKS when GPU nodes are present

* ✅ No manual installation required
* ✅ Exposes `nvidia.com/gpu` resource to Kubernetes scheduler

#### Pod Disruption Budget

* For scale-to-zero workers: `pdb.minAvailable: 0`

* For high-availability workers: `pdb.minAvailable: 1` + deploy 2+ replicas

#### Single Pod per Node

* Current configuration: **1 GPU node max**

* Only **1 knowledge worker pod** runs at a time
* For parallel processing, increase:
  * `autoscaling.maxReplicas` in values.yaml
  * Node pool max node count in Azure

#### Startup Time

* **Cold start** (from zero): 3-5 minutes (node provisioning + pod startup)

* **Warm start** (node exists): ~30 seconds

---

## Build & Push Image (ACR)

```powershell
az acr login --name copilotgtmacrdev
docker build -t copilotgtmacrdev.azurecr.io/copilot-backend:$Env:TAG .
docker push copilotgtmacrdev.azurecr.io/copilot-backend:$Env:TAG
```

## Helm Deploy / Upgrade (manual example)

```powershell
helm upgrade --install copilot-backend ./application `
  -n copilotgtm `
  --create-namespace `
  --set api.image.repository=copilotgtmacrdev.azurecr.io/copilot-backend `
  --set api.image.tag=$Env:TAG `
  --set knowledge-extract-worker.image.repository=copilotgtmacrdev.azurecr.io/copilot-backend `
  --set knowledge-extract-worker.image.tag=$Env:TAG `
  --set knowledge-upload-worker.image.repository=copilotgtmacrdev.azurecr.io/copilot-backend `
  --set knowledge-upload-worker.image.tag=$Env:TAG `
  --set organization-worker.image.repository=copilotgtmacrdev.azurecr.io/copilot-backend `
  --set organization-worker.image.tag=$Env:TAG `
  --set chat-worker.image.repository=copilotgtmacrdev.azurecr.io/copilot-backend `
  --set chat-worker.image.tag=$Env:TAG `
  --set integrations-worker.image.repository=copilotgtmacrdev.azurecr.io/copilot-backend `
  --set integrations-worker.image.tag=$Env:TAG `
  --set global.redis.host=$Env:REDIS_HOST `
  --set global.redis.port=$Env:REDIS_PORT `
  --set global.redis.enableTLS=true `
  --set global.redis.usePassword=true `
  --set api.secrets.REDIS_HOST=$Env:REDIS_HOST `
  --set api.secrets.REDIS_PORT=$Env:REDIS_PORT `
  --set api.secrets.REDIS_PASSWORD=$Env:REDIS_PASSWORD
```

## Verify Deployment

```powershell
kubectl get pods -n copilotgtm
kubectl get deployments -n copilotgtm | findstr backend
kubectl get scaledobjects -n copilotgtm
helm status copilot-backend -n copilotgtm
```

Sample output (workers scaled to zero except upload worker min=1):

```text
PS> kubectl get scaledobjects -n copilotgtm
NAME                                              SCALETARGETNAME                             READY   ACTIVE   MIN  MAX  TRIGGERS
copilot-backend-knowledge-extract-worker-scaler   copilot-backend-knowledge-extract-worker    True    False    0    5    redis
copilot-backend-knowledge-upload-worker-scaler    copilot-backend-knowledge-upload-worker     True    False    1    10   redis
copilot-backend-organization-worker-scaler        copilot-backend-organization-worker         True    False    0    5    redis
copilot-backend-chat-worker-scaler                copilot-backend-chat-worker                 True    False    0    5    redis
```

## Forcing a Worker Pod (debug)

Scale a single worker manually (e.g. chat):

```powershell
kubectl scale deploy copilot-backend-chat-worker -n copilotgtm --replicas=1
```

Or temporarily raise minReplicas via Helm:

```powershell
helm upgrade copilot-backend ./application -n copilotgtm --set chat-worker.autoscaling.minReplicas=1
```

## Trigger a Scale-Up (enqueue test)

From any pod with redis-cli (or port forward):

```powershell
redis-cli -h $Env:REDIS_HOST -p $Env:REDIS_PORT -a $Env:REDIS_PASSWORD --tls LPUSH knowledge_upload test-item
```

Within one polling interval the corresponding worker ScaledObject should become ACTIVE=True and replicas increase.

## KEDA / Redis Troubleshooting

```powershell
kubectl describe scaledobject copilot-backend-knowledge-extract-worker-scaler -n copilotgtm
kubectl logs -n keda -l app=keda-operator --tail=200 | findstr /i redis
kubectl get triggerauthentication -n copilotgtm
kubectl get secret copilot-backend-secret -n copilotgtm -o yaml | findstr REDIS_PASSWORD
```

Common READY=False causes:

1. Wrong Redis host/port (DNS errors)
2. Auth failure (NOAUTH) – password missing or mismatch
3. Network timeout (firewall / private endpoint)
4. TLS flag mismatch (enableTLS must reflect port 6380 for Azure Redis)

## Remove Release

```powershell
helm uninstall copilot-backend -n copilotgtm
```

## List Releases / Resources

```powershell
helm list -n copilotgtm
kubectl get all -n copilotgtm
```

## Startup Job Logs

```powershell
kubectl logs job/copilot-backend-startup -n copilotgtm
```

## WebSocket Support

Ingress annotations already include timeouts & websocket support; see `application/values.yaml` under `api.ingress.annotations`.

## Cert-Manager / TLS

Cert-manager and TLS issuers are managed by Terraform under
`infra/terraform/azure/<env>/addons.tf`. Do not apply legacy standalone
ClusterIssuer manifests from the Helm chart.

For dev Envoy validation, Terraform creates a DNS-01 issuer and the
`devapi-envoy-tls` certificate. Check status with:

```powershell
kubectl describe clusterissuer letsencrypt-dns01
kubectl get certificate -n copilotgtm
```

## Debugging Pods

```powershell
kubectl describe pod <pod> -n copilotgtm
kubectl logs <pod> -n copilotgtm
```

## Notes

* Workers may legitimately show no pods (replicas=0) when queues are empty.
* Use unique image tags (build IDs) + pullPolicy IfNotPresent.
* Keep global.redis.* in Helm values for KEDA; env secret values power Celery itself.
