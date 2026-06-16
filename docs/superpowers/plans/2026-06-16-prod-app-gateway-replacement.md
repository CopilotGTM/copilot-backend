# Prod App Gateway Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace production Azure Application Gateway/AGIC with Envoy Gateway + Kubernetes Gateway API in one maintenance-window rollout.

**Architecture:** Infra creates Envoy Gateway, moves `api.copilotgtm.com` DNS to Envoy, changes cert-manager to DNS-01, disables AGIC, and deletes App Gateway. The backend deploy then renders the final HTTPS Gateway/HTTPRoute and disables the legacy App Gateway Ingress. Backend Python code, public hostname, OAuth URLs, webhook URLs, workers, CronJobs, and KEDA resources do not change.

**Tech Stack:** AKS, Terraform, Azure DNS, Azure Standard Load Balancer, Envoy Gateway Helm chart, Kubernetes Gateway API, cert-manager DNS-01, Helm.

---

## Verified Prod State Before This Change

- Prod API hostname: `api.copilotgtm.com`.
- Current DNS result: `172.168.104.250`.
- `172.168.104.250` is App Gateway public IP `copilot-gtm-waf-prod-pip`.
- App Gateway: `copilot-gtm-waf-prod`, resource group `copilot-gtm-app-prod`, SKU `Standard_v2`.
- AKS: `copilot-gtm-aks-prod`, namespace `copilotgtm`.
- AKS AGIC is enabled and points to `copilot-gtm-waf-prod`.
- Live Ingress: `copilot-backend`, class `azure-application-gateway`, host `api.copilotgtm.com`.
- Live certificate: `api-tls`, secret `api-tls`, Ready `True`.
- Gateway API CRDs were not installed in prod when checked.

## One-Go Rollout Decision

The safer production migration would be phased: temporary Envoy hostname, then DNS cutover, then App Gateway deletion. That is not the chosen execution mode.

Chosen mode:

1. Run production infra pipeline after 7 PM.
2. Infra installs Envoy Gateway and Gateway API CRDs.
3. Infra creates a dedicated static Envoy public IP in the AKS node resource group.
4. Infra points `api.copilotgtm.com` to Envoy.
5. Infra switches `api-tls` to DNS-01.
6. Infra disables AKS AGIC with `app_gateway_id = null`.
7. Infra deletes App Gateway, App Gateway public IP, and AGIC role assignments.
8. Immediately run production backend pipeline.
9. Backend renders final Gateway API resources for `api.copilotgtm.com`.
10. Validate public traffic.

Accepted maintenance-window risk:

- There can be downtime between infra completion and backend completion because DNS points to Envoy before the backend release creates the final `Gateway` and `HTTPRoute`.
- Do not start the infra pipeline unless the backend pipeline is ready to run immediately after.
- Do not use `helm --force`; normal Helm reconciliation must work.

## Final Traffic Path

```text
User / integration provider
  -> Azure DNS api.copilotgtm.com
  -> AKS Standard Load Balancer public IP dedicated to Envoy
  -> Envoy Gateway
  -> Gateway API Gateway listener https:443
  -> Gateway API HTTPRoute
  -> copilot-backend ClusterIP service:8080
  -> copilot-backend API pod
```

TLS is still HTTPS for users. The `HTTPRoute` object name does not mean public HTTP; it is the Kubernetes Gateway API L7 route resource. Public traffic terminates TLS at Envoy using `api-tls`, then forwards to the backend service on port `8080` inside the cluster.

## Files Changed

### `infra/terraform/azure/prod/main.tf`

- Removed `module "waf"`.
- Set AKS `app_gateway_id = null`.
- Removed `module.waf` from AKS `depends_on`.
- Removed:
  - `azurerm_role_assignment.agic_appgw_contributor`
  - `azurerm_role_assignment.agic_rg_reader`
  - `azurerm_role_assignment.agic_vnet_network_contributor`

### `infra/terraform/azure/prod/addons.tf`

- Added `envoy-gateway-system` namespace with label `name=envoy-gateway-system`.
- Added dedicated static Standard public IP `copilot-gtm-envoy-gateway-prod-pip`.
- Added pinned Envoy Gateway Helm release `v1.8.1`.
- Added DNS-01-only `ClusterIssuer` named `letsencrypt-dns01`.
- Added prod `EnvoyProxy` named `copilot-backend-prod`.
- Added `GatewayClass` named `envoy-gateway-prod`.
- Changed `api` DNS A record to the Envoy public IP.
- Added temporary `api-envoy` DNS/certificate resources for troubleshooting/cleanup use.
- Changed `api-tls` certificate to use `letsencrypt-dns01`.

### `infra/terraform/azure/prod/variables.tf`

- Added Envoy variables:
  - `enable_envoy_gateway`
  - `envoy_gateway_namespace`
  - `envoy_gateway_chart_version`
  - `envoy_gateway_public_ip_name`
  - `envoy_gateway_class_name`
  - `envoy_validation_subdomain`
- Added `azure_resource_owner_object_id` to pin existing prod ownership/admin object IDs and avoid unrelated Entra/Key Vault churn when a different local operator runs Terraform.
- Removed unused App Gateway variables:
  - `waf_name`
  - `appgw_sku`
- Kept `vnet_waf_subnet` temporarily to avoid unrelated VNet address-space churn.

### `infra/terraform/azure/prod/terraform.tfvars`

- Removed `appgw_sku`.
- Added `azure_resource_owner_object_id = "4c81e541-7b3f-4d76-bcca-a04da9a15962"` to preserve current prod Key Vault admin and app owner assignments.
- Added explicit prod Envoy settings:
  - `enable_envoy_gateway = true`
  - `envoy_gateway_class_name = "envoy-gateway-prod"`
  - `envoy_gateway_public_ip_name = "copilot-gtm-envoy-gateway-prod-pip"`
  - `envoy_validation_subdomain = "api-envoy"`

### `infra/terraform/azure/prod/outputs.tf`

- Added `aks_node_resource_group`.
- Added `envoy_gateway_public_ip`.

### `charts/application/values-prod.yaml`

Prod now explicitly overrides dev Gateway defaults and enables Envoy-to-API NetworkPolicy access:

```yaml
api:
  gateway:
    enabled: true
    mode: cutover-complete
    disableLegacyIngress: true
    gatewayName: copilot-backend
    gatewayClassName: envoy-gateway-prod
    host: api.copilotgtm.com
    tlsSecretName: api-tls
    routeName: copilot-backend
    timeouts:
      request: "3600s"
      backendRequest: ""
    validation:
      enabled: false
      gatewayName: copilot-backend-envoy-validation
      host: api-envoy.copilotgtm.com
      tlsSecretName: api-envoy-tls
      routeName: copilot-backend-envoy-validation
  networkPolicy:
    envoyGateway:
      enabled: true
```

## Loopholes Found And Resolved

| Loophole | Resolution |
| --- | --- |
| Prod had no Gateway API CRDs | Infra installs Envoy Gateway before backend deploy; backend deploy must run only after infra succeeds. |
| Prod chart could inherit dev Gateway defaults | `values-prod.yaml` now explicitly sets prod host, class, TLS secret, route, and mode. |
| DNS could move before backend route exists | Accepted as maintenance-window downtime; backend must run immediately after infra. |
| App Gateway HTTP-01 cert renewal would disappear | `api-tls` now uses DNS-01 issuer. |
| Envoy could be blocked by NetworkPolicy | Prod now explicitly sets `api.networkPolicy.envoyGateway.enabled=true`, and Terraform labels the Envoy namespace `name=envoy-gateway-system`. |
| Prod initially missed a dev cutover value | Added the missing prod `api.networkPolicy.envoyGateway.enabled=true` override after comparing dev/prod. |
| Long requests could timeout differently than App Gateway | HTTPRoute renders `timeouts.request: "3600s"`. |
| Helm `--force` could hide resource lifecycle bugs | Not used; normal `helm upgrade --install` remains. |
| Validation Gateway could mutate into final Gateway | Final and validation Gateway objects are separate; validation is disabled for prod final cutover. |
| Temporary validation resources could create cleanup cycles | `api-envoy` cleanup is deferred to a separate later apply. |
| App Gateway rollback is slower after deletion | Accepted for this one-go cost-removal rollout; primary recovery is fixing Envoy/Gateway state. |
| Prod Terraform tried to rotate unrelated Key Vault admins and Entra app owners when run by a different local identity | Prod now pins `azure_resource_owner_object_id` to the current live owner/admin object ID. |
| Prod Terraform tried to replace the Teams Bot because its default endpoint differed from live state | Prod `terraform.tfvars` now pins `teams_bot_messaging_endpoint` to the current live `/users/bots/teams/messages` endpoint. |

## Pre-Deploy Verification

Run locally before pushing:

```powershell
terraform fmt -recursive infra\terraform\azure\prod
terraform -chdir=infra\terraform\azure\prod validate
terraform -chdir=infra\terraform\azure\prod plan -refresh=false
helm lint charts\application -f charts\application\values.yaml -f charts\application\values-prod.yaml
helm template copilot-backend charts\application -n copilotgtm -f charts\application\values.yaml -f charts\application\values-prod.yaml --show-only charts/api/templates/gateway.yaml
helm template copilot-backend charts\application -n copilotgtm -f charts\application\values.yaml -f charts\application\values-prod.yaml --show-only charts/api/templates/httproute.yaml
helm template copilot-backend charts\application -n copilotgtm -f charts\application\values.yaml -f charts\application\values-prod.yaml | Select-String "kind: Ingress|azure-application-gateway|api-envoy.copilotgtm.com"
```

Expected:

- Terraform validate succeeds.
- Terraform plan summary is scoped to ingress: `8 to add, 4 to change, 5 to destroy`.
- Terraform plan adds Envoy resources and destroys App Gateway resources.
- Terraform plan changes AKS only by removing `ingress_application_gateway`.
- Terraform plan does not replace Teams Bot, Key Vault admin assignments, or Entra app owners.
- Helm lint succeeds.
- Gateway renders hostname `api.copilotgtm.com`.
- Gateway renders class `envoy-gateway-prod`.
- Gateway renders TLS secret `api-tls`.
- HTTPRoute renders host `api.copilotgtm.com`.
- HTTPRoute renders `timeouts.request: 3600s`.
- Legacy Ingress does not render.
- `azure-application-gateway` does not render.
- `api-envoy.copilotgtm.com` does not render in backend chart because validation is disabled.

## Deploy Runbook

1. Start after 7 PM maintenance window.
2. Run prod infra pipeline.
3. Confirm infra succeeds.
4. Immediately run prod backend pipeline.
5. Do not run unrelated pipelines between infra and backend.

Post-infra checks:

```powershell
az aks show --name copilot-gtm-aks-prod --resource-group copilot-gtm-app-prod --query "addonProfiles.ingressApplicationGateway"
az network application-gateway show --name copilot-gtm-waf-prod --resource-group copilot-gtm-app-prod
Resolve-DnsName api.copilotgtm.com
```

Expected:

- AGIC is disabled or absent.
- App Gateway lookup returns not found.
- DNS resolves to the new Envoy public IP.

Post-backend checks:

```powershell
kubectl --context copilot-gtm-aks-prod get gateway,httproute -n copilotgtm
kubectl --context copilot-gtm-aks-prod get svc -n envoy-gateway-system
kubectl --context copilot-gtm-aks-prod get certificate -n copilotgtm api-tls
kubectl --context copilot-gtm-aks-prod get ingress -n copilotgtm
curl.exe -vk https://api.copilotgtm.com/health
```

Expected:

- Gateway is programmed.
- HTTPRoute is accepted.
- Envoy service has the Terraform Envoy public IP.
- `api-tls` is ready.
- No legacy `copilot-backend` Ingress exists.
- `/health` returns:

```json
{"status":"healthy","service":"Copilot Backend","version":"1.0.0"}
```

## Business Flow Smoke Tests

Run these after `/health`:

- Login/auth `/users/auth/me`.
- Frontend API call from `https://app.copilotgtm.com`.
- Microsoft OAuth callback URL remains `https://api.copilotgtm.com/users/integrations/copilot-oauth/callback`.
- Google Chat audience remains `https://api.copilotgtm.com/users/bots/google-chat/events`.
- Slack bot callback remains on `https://api.copilotgtm.com`.
- HubSpot webhook remains `https://api.copilotgtm.com/users/integrations/hubspot/webhook`.
- Microsoft Graph webhook base remains `https://api.copilotgtm.com`.
- Salesforce callback remains `https://api.copilotgtm.com/users/integrations/copilot-oauth/callback`.

## Rollback

After this one-go rollout deletes App Gateway, rollback is not an instant DNS flip.

Fast recovery path:

1. Inspect Gateway:
   `kubectl --context copilot-gtm-aks-prod describe gateway copilot-backend -n copilotgtm`
2. Inspect HTTPRoute:
   `kubectl --context copilot-gtm-aks-prod describe httproute copilot-backend -n copilotgtm`
3. Inspect Envoy service/events:
   `kubectl --context copilot-gtm-aks-prod get svc -n envoy-gateway-system`
4. Inspect backend NetworkPolicy.
5. Fix Envoy/Gateway/route state and rerun backend pipeline if needed.

Slow rollback path:

1. Recreate `module "waf"`.
2. Pass `app_gateway_id = module.waf.waf_id`.
3. Restore AGIC role assignments.
4. Re-enable legacy Ingress.
5. Point `api.copilotgtm.com` back to the recreated App Gateway public IP.
6. Run infra and backend pipelines.

## Completion Criteria

- Prod App Gateway resource is deleted.
- AKS AGIC is disabled.
- `api.copilotgtm.com` resolves to Envoy public IP.
- Gateway API resources are accepted/programmed.
- Public `/health` works.
- Auth, frontend, OAuth, and webhook smoke tests pass.
- No legacy App Gateway Ingress renders.
- Next Azure billing export stops App Gateway fixed/capacity charges.
