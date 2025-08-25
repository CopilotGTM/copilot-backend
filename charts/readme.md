# Application Chart & Workers

This Helm umbrella chart deploys:
* API (FastAPI webservice)
* Celery workers (knowledge-extract, knowledge-upload, organization, chat)
* KEDA ScaledObjects for queue length–based autoscaling (Redis)
* A startup Job (migrations + seeding)

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

## Cert-Manager (Production TLS)

Install cert-manager and apply the `cert-manager-prod.yaml` in the `webservice` subchart (update email first):
```powershell
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl apply -f application/charts/webservice/cert-manager-prod.yaml
```

Check status:
```powershell
kubectl describe clusterissuer letsencrypt-prod
kubectl get certificate -A | findstr copilot
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

## Production HTTPS & Let's Encrypt (Cert-Manager) Setup

This project uses cert-manager to automatically provision and renew browser-trusted TLS certificates from Let's Encrypt.

### Install cert-manager

```sh
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
```

### Create ClusterIssuer & Certificate

**Edit `charts/copilot-backend/cert-manager-prod.yaml` and update the email under `spec.acme.email`.**

```sh
kubectl apply -f charts/copilot-backend/cert-manager-prod.yaml
```

This will:
- Register a Let's Encrypt ACME account
- Issue a trusted certificate for your domain (`api.copilotgtm.com`)
- Store keys as the Kubernetes TLS secret `api-copilotgtm-com-tls` (referenced by Ingress for HTTPS)

### Monitor Status

```sh
kubectl describe clusterissuer letsencrypt-prod
kubectl describe certificate api-copilotgtm-com -n copilot
kubectl get secret api-copilotgtm-com-tls -n copilot
```

Certificates renew automatically before expiry.
---
## WebSocket Support

This deployment is configured to support WebSocket upgrades via the NGINX ingress controller.  
If your FastAPI app exposes WebSocket paths, they will tunnel correctly through the public HTTPS endpoint.

**Key ingress annotations (see `charts/copilot-backend/values.yaml`):**
- `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"`
- `nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"`
- `nginx.ingress.kubernetes.io/proxy-body-size: "10m"`
- `nginx.ingress.kubernetes.io/websocket-services: ...`

**Test WebSocket connectivity:**
Example using `wscat`:
```sh
wscat -c wss://api.copilotgtm.com/ws-endpoint
```
Replace `/ws-endpoint` with your actual FastAPI websocket route.

---

## Notes

- Make sure you set the correct namespaces and paths according to your environment.
- For debugging failed pods, use:
  ```sh
  kubectl describe pod <pod-name> -n copilotgtm
  kubectl logs <pod-name> -n copilotgtm