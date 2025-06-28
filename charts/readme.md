# Technical Deck - Pipeline

## Build & Push Docker Image to Azure Container Registry (ACR)

Follow these steps to build your Docker image and push it to your Azure Container Registry before deploying via Helm:

```sh
# 1. Log in to Azure and your ACR
az acr login --name copilottechnicaldeck

# 2. Build your Docker image locally with the correct tag
docker build -t copilottechnicaldeck.azurecr.io/technical-deck-app:latest .

# 3. Push the image to your ACR
docker push copilottechnicaldeck.azurecr.io/technical-deck-app:latest
```

## Deploy with Helm

Once the image is pushed, you can use these Helm commands to manage the deployment in Kubernetes.

### Install or Upgrade the Chart

```sh
helm upgrade --install technical-deck charts/technical-deck --values charts/technical-deck/values.yaml --namespace copilot --set image.tag=1
```

### Delete the Release

This removes all Kubernetes resources managed by this Helm release:

```sh
helm uninstall technical-deck --namespace copilot
```

### Describe the Helm Release

Get detailed status of the Helm release and its resources:

```sh
helm status technical-deck --namespace copilot
```

### List Helm Releases

```sh
helm list --namespace copilot
```

### Get All Resources in the Namespace

```sh
kubectl get all -n copilot
```
## Production HTTPS & Let's Encrypt (Cert-Manager) Setup

This project uses cert-manager to automatically provision and renew browser-trusted TLS certificates from Let's Encrypt.

### Install cert-manager

```sh
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
```

### Create ClusterIssuer & Certificate

**Edit `charts/technical-deck/cert-manager-prod.yaml` and update the email under `spec.acme.email`.**

```sh
kubectl apply -f charts/technical-deck/cert-manager-prod.yaml
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

**Key ingress annotations (see `charts/technical-deck/values.yaml`):**
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
  kubectl describe pod <pod-name> -n copilot
  kubectl logs <pod-name> -n copilot