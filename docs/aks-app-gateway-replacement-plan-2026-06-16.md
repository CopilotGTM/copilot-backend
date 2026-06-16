# AKS App Gateway Replacement Plan

Date: 2026-06-16
Environment: dev AKS, `copilot-gtm-aks-dev`
Decision: use Envoy Gateway + Kubernetes Gateway API, not nginx Ingress.
Goal: remove the dev Application Gateway cost by routing `devapi.copilotgtm.com`
through an open-source in-cluster gateway first, then optionally add Cloudflare
later.
Current scope: complete Flow 1 only. Do not implement Cloudflare DNS/proxy or
Cloudflare Tunnel in this phase.

Implementation stance: dev validation is complete enough to remove the
Application Gateway cost driver. The next main deploy should keep Envoy as the
only dev public ingress path, disable AKS AGIC, destroy the dev Application
Gateway resources, and keep the source/backend Python code unchanged.

## Executive Summary

Replace the current Azure Application Gateway ingress path with Envoy Gateway
running inside AKS. Envoy Gateway exposes Envoy through the AKS Standard Load
Balancer and routes traffic with Gateway API resources: `GatewayClass`,
`Gateway`, and `HTTPRoute`.

This keeps backend Python code, service names, pods, workers, KEDA, CronJobs,
and public API hostname unchanged. The backend chart changes from rendering only
old Kubernetes `Ingress` resources to rendering Gateway API resources for the
new path.

Cloudflare is not part of version 1. Version 2 can later place Cloudflare in
front of Envoy Gateway. Cloudflare is the edge/WAF/CDN layer; Envoy remains the
origin router unless we later choose Cloudflare Tunnel direct-to-service.

Flow 1 for this implementation is:

```text
Azure DNS
  -> AKS Standard Load Balancer public IP
  -> Envoy Gateway
  -> Gateway API HTTPRoute
  -> copilot-backend service
  -> API pod
```

Do not add `cloudflared`, Cloudflare DNS records, Cloudflare WAF rules, or
Cloudflare Tunnel resources while completing Flow 1.

Current main-step rollout for dev:

1. Infra pipeline disables AKS AGIC and destroys the dev Application Gateway,
   its public IP, and the AGIC role assignments.
2. Infra keeps the existing Envoy Gateway public IP, GatewayClass, DNS-01
   issuer, and `devapi.copilotgtm.com` A record pointing at Envoy.
3. Backend pipeline deploys in `cutover-complete` mode and renders only the
   Gateway API route for `devapi.copilotgtm.com`.
4. Backend pipeline uses the normal `helm upgrade --install` path. Validation
   and final cutover use separate Gateway objects so Helm does not have to
   mutate a validation listener into the production listener.
5. After the main deploy is verified, run a separate cleanup apply for the old
   `devapi-envoy` validation DNS/certificate resources if they are no longer
   needed.

## Current Verified State

Live context:

- Kubernetes context: `copilot-gtm-aks-dev`
- Helm release: `copilot-backend`, namespace `copilotgtm`, revision `522`
- Current dev API DNS:
  - `devapi.copilotgtm.com` resolves to Envoy public IP `20.12.200.117`.
  - `devapi-envoy.copilotgtm.com` also resolves to `20.12.200.117`.
- Current Kubernetes ingress:
  - No `copilot-backend` Kubernetes `Ingress` remains in namespace
    `copilotgtm`.
  - Gateway API is the intended runtime surface for the dev API.
- Current Application Gateway:
  - Name: `copilot-gtm-waf-dev`
  - SKU: `Standard_v2`
  - State: `Running`
  - Public IP resource: `copilot-gtm-waf-dev-pip`
  - Public IP: `20.118.21.159`
  - Status for this plan: remove from dev Terraform to stop the fixed/capacity
    charge.
- Current AKS networking:
  - AKS network profile uses Standard Load Balancer.
  - AKS already has a Standard Load Balancer named `kubernetes` in the node
    resource group.
  - Existing AKS Load Balancer public IP: `130.131.152.31`. This must not be
    reused blindly for public API DNS. The migration should allocate a
    dedicated static Envoy Gateway IP and bind Envoy to that IP.
- Current cert-manager state:
  - `devapi-tls` certificate is ready for `devapi.copilotgtm.com`.
  - `devapi-envoy-tls` certificate is ready for the temporary validation host.
  - Current `ClusterIssuer` includes an HTTP-01 solver tied to
    `azure-application-gateway` plus Azure DNS-01 support.
  - Envoy-facing certificates use DNS-01 and should not depend on the App
    Gateway HTTP-01 solver.

Latest live validation observations:

- In-cluster service health works:
  `http://copilot-backend:8080/health` returned HTTP 200.
- In-cluster Envoy validation path works when resolving the hostname to
  `20.12.200.117`: `https://devapi-envoy.copilotgtm.com/health` returned HTTP
  200.
- The Helm release manifest already contained the desired main Gateway listener
  for `devapi.copilotgtm.com`, but the live Gateway object remained stale on the
  old `devapi-envoy` validation listener. The chart now avoids this pattern by
  using a separate validation Gateway object.
- External TCP checks to `20.12.200.117:443` failed from the local machine before
  the next backend redeploy. Retest after the backend pipeline reconciles the
  Gateway object.

Latest Terraform dry-run for the App Gateway removal:

- `terraform plan -refresh=false` in `infra/terraform/azure/dev` succeeded.
- Plan summary: `0 to add, 1 to change, 5 to destroy`.
- The in-place change removes the AKS `ingress_application_gateway` block.
- The destroys are:
  - `module.waf.azurerm_application_gateway.waf`
  - `module.waf.azurerm_public_ip.waf`
  - `azurerm_role_assignment.agic_appgw_contributor`
  - `azurerm_role_assignment.agic_rg_reader`
  - `azurerm_role_assignment.agic_vnet_network_contributor`
- The legacy WAF subnet CIDR remains in the VNet module input to avoid VNet
  address-space churn while deleting the gateway.
- The temporary `devapi-envoy` DNS/certificate resources intentionally remain in
  this apply. Removing them in the same plan as AGIC/App Gateway removal caused
  a Terraform provider dependency cycle through the Kubernetes provider.

Resolved implementation constraints:

- Envoy Gateway controller runs in `envoy-gateway-system`.
- Application `Gateway` and `HTTPRoute` resources should be rendered in
  `copilotgtm`, the same namespace as `copilot-backend` and its TLS secrets.
  This avoids cross-namespace TLS secret references and avoids needing
  `ReferenceGrant` for the initial migration.
- Envoy data-plane pods may run outside `copilotgtm`; the backend NetworkPolicy
  must explicitly allow Envoy-to-backend traffic on port `8080`.
- Gateway API CRDs must be installed by the infra pipeline before the backend
  pipeline renders `Gateway` or `HTTPRoute`.
- The first implementation should pin one Envoy Gateway Helm chart version in
  Terraform. Do not use an unpinned chart or `latest`.
- HTTP timeout handling should use Gateway API `HTTPRoute` rule timeouts for
  HTTP routes. The Envoy Gateway documentation shows
  `rules[].timeouts.request` and optional `rules[].timeouts.backendRequest`.
  Do not implement HTTP timeout handling as a vague future policy.

June CSV cost evidence from `AzureUsage (5).csv`:

| Item | Quantity | Cost |
| --- | ---: | ---: |
| Application Gateway, Standard Fixed Cost | 728.00 | $179.09 |
| Application Gateway, Standard Capacity Units | 1275.57 | $10.20 |
| Load Balancer, Standard Included LB Rules and Outbound Rules | 728.00 | $18.20 |
| Load Balancer, Standard Data Processed | 0.00 | $0.00 |
| Virtual Network, Standard IPv4 Static Public IP | 2912.00 | $14.56 |

Conclusion: the Standard Load Balancer line is already present. Moving from App
Gateway to Envoy Gateway should remove the dev App Gateway fixed/capacity cost,
while keeping a much smaller Load Balancer/Public IP cost.

Cost attribution caveat: the CSV is subscription-scoped, not resource-scoped.
Before quoting a final monthly saving, split the Application Gateway row by
resource using Cost Management resource-level export or live resource inventory
plus retail rates. The current row proves App Gateway is a real fixed-cost
target, but it does not prove the whole row belongs only to dev.

## Version 1 Architecture: Azure DNS + Envoy Gateway

Traffic path:

```text
User / webhook provider
  -> Azure DNS: devapi.copilotgtm.com
  -> Azure public IP attached to AKS Standard Load Balancer
  -> Envoy Gateway managed Envoy service
  -> Gateway API Gateway listener
  -> HTTPRoute
  -> copilot-backend ClusterIP service
  -> copilot-backend API pod
```

This is the immediate cost-reduction architecture. It removes Application
Gateway from the request path but keeps the public hostname and backend API
contract unchanged.

Expected implementation changes:

1. Add Terraform-managed Envoy Gateway Helm release in
   `infra/terraform/azure/dev/addons.tf`.
2. Add a dedicated static Standard public IP for Envoy and bind Envoy's
   LoadBalancer service to that IP.
3. Add `GatewayClass` with an `EnvoyProxy` parameters reference so we can set
   the Envoy service type and Azure Load Balancer annotations.
4. Render the application `Gateway` and `HTTPRoute` in namespace `copilotgtm`.
5. Keep the main Gateway API route for `devapi.copilotgtm.com`.
6. Keep Azure DNS `devapi` pointed at the dedicated Envoy public IP.
7. Disable the old App Gateway `Ingress` path for dev in Helm values.
8. Remove the AKS AGIC add-on connection and App Gateway Terraform module usage
   from dev.
9. Delete the dev Application Gateway and its public IP through the infra
   pipeline.
10. Remove the temporary `devapi-envoy` validation host only in a later cleanup
    apply.

No backend Python code should change.

## Version 2 Architecture: Cloudflare In Front

Traffic path if Cloudflare is added later as an edge proxy:

```text
User / webhook provider
  -> Cloudflare DNS/proxy/WAF
  -> Azure public IP attached to AKS Standard Load Balancer
  -> Envoy Gateway managed Envoy service
  -> Gateway API Gateway listener
  -> HTTPRoute
  -> copilot-backend ClusterIP service
  -> copilot-backend API pod
```

Cloudflare is mostly an add-on in this version. It is not part of Flow 1:

- DNS authority moves from Azure DNS to Cloudflare.
- Cloudflare proxy/WAF/rate-limit rules become the public edge.
- Envoy Gateway remains the Kubernetes origin router.
- Backend code remains unchanged.
- Gateway/HTTPRoute rules mostly remain unchanged.

## Future Optional Architecture: Cloudflare Tunnel

This is a separate later design, not part of Flow 1 and not part of the current
implementation.

Tunnel traffic path:

```text
User
  -> Cloudflare
  -> cloudflared pod inside AKS
  -> copilot-backend ClusterIP service
  -> copilot-backend API pod
```

That tunnel design can remove the public Azure ingress IP and may remove Envoy
for a single-service API. It is not needed for Flow 1. If we have multiple
services, route rules, canary, headers, long timeouts, or internal origin
routing, keeping Envoy behind Cloudflare remains cleaner.

## Files To Change During Implementation

### Helm chart

`charts/application/values.yaml`

- Keep the existing App Gateway ingress template for other environments and for
  source-level rollback, but dev values should not render it after this cutover.
- Keep the Gateway API values block:
  - `api.gateway.enabled`
  - `api.gateway.mode`: `disabled`, `validation`, `cutover-prepare`,
    `cutover-complete`, or `rollback`
  - `api.gateway.gatewayName`
  - `api.gateway.gatewayClassName`
  - `api.gateway.host`
  - `api.gateway.tlsSecretName`
  - `api.gateway.routeName`
  - `api.gateway.timeouts.request`
  - `api.gateway.timeouts.backendRequest`
  - `api.gateway.validation.enabled`
  - `api.gateway.validation.host`
  - `api.gateway.validation.tlsSecretName`
  - `api.gateway.disableLegacyIngress`
- Dev should run with:
  - `api.gateway.enabled=true`
  - `api.gateway.mode=cutover-complete`
  - `api.gateway.validation.enabled=false`
  - `api.gateway.disableLegacyIngress=true`
- The backend deploy command should not use `--force` as the default path. If a
  stale Gateway object remains after normal Helm reconciliation, inspect and fix
  the specific Gateway/HTTPRoute state instead of making forced replacement the
  standard deploy behavior.

`charts/application/charts/webservice/templates/ingress.yaml`

- Keep this template for prod and possible rollback source code.
- Dev values should not render this template once App Gateway is deleted.

Gateway chart templates under
`charts/application/charts/webservice/templates/`:

- `gateway.yaml`
  - Renders `gateway.networking.k8s.io/v1` `Gateway`.
  - Namespace: `copilotgtm`.
  - Listener `https` on port `443`.
  - Hostname from values.
  - TLS mode `Terminate`.
  - Certificate ref points to the cert-manager secret.
- `httproute.yaml`
  - Renders `gateway.networking.k8s.io/v1` `HTTPRoute`.
  - Namespace: `copilotgtm`.
  - `parentRefs` points to the `Gateway`.
  - `parentRefs.namespace` should be omitted because the route and gateway live
    in the same namespace.
  - Hostname from values.
  - Path prefix `/`.
  - Backend ref points to `copilot-backend` service port `8080`.
- `httproute-validation.yaml`
  - Same backend service.
  - Hostname `devapi-envoy.copilotgtm.com`.
- Parent ref points to a separate validation Gateway named
  `copilot-backend-envoy-validation`.
  - Already used during validation. Keep it disabled in dev values now, and
    delete its DNS/certificate resources in a later Terraform cleanup apply.

`charts/application/charts/webservice/templates/gateway-validation.yaml`

- Renders a separate validation-only `Gateway`.
- Name comes from `api.gateway.validation.gatewayName`.
- Listener name is `https-validation`.
- Hostname is `devapi-envoy.copilotgtm.com`.
- This prevents the production Gateway object from being reused for temporary
  validation traffic.

Important sequencing constraint:

- The older safety sequence was: validate temporary host, enable main Envoy
  route, move DNS, then disable the App Gateway ingress. Dev has already moved
  past that sequence.
- For future prod migration, do not reuse the final Gateway object for
  validation. Render `copilot-backend-envoy-validation` for the temporary host
  and `copilot-backend` for the real host.
- Current sequence is infra removal first, then normal backend Helm deploy, then
  live verification.
- If backend verification fails after App Gateway deletion, rollback is no
  longer instant DNS flipback. Recreate/re-enable the App Gateway path or fix the
  Gateway object directly.
- After the cutover is stable, remove the temporary validation host in a
  separate cleanup apply to keep the public surface small.

`charts/application/charts/webservice/templates/networkpolicy.yaml`

- Add a values-driven allow rule for Envoy Gateway traffic to API port `8080`.
- Required implementation:
  - label the Envoy namespace from Terraform with
    `name: envoy-gateway-system`
  - add `api.networkPolicy.envoyGateway.enabled`
  - add `api.networkPolicy.envoyGateway.namespaceSelector.matchLabels.name`
    defaulting to `envoy-gateway-system`
  - allow TCP `8080`
- Existing App Gateway/VNet allowance can remain harmlessly until a later chart
  cleanup, but dev traffic should no longer depend on it.

Do not rely only on the broad `10.0.0.0/16` `ipBlock` rule. Envoy pod source IP
behavior can vary by CNI/load-balancing path, and a namespace/pod allow rule is
clearer.

Verification requirement:

- After infra apply, confirm the namespace has the expected label:
  `kubectl get namespace envoy-gateway-system --show-labels`.
- After backend deploy, confirm the NetworkPolicy contains a namespace selector
  for `name=envoy-gateway-system`.

### Terraform dev infra

`infra/terraform/azure/dev/addons.tf`

- Add `kubernetes_namespace_v1 "envoy_gateway_system"` with label
  `name = "envoy-gateway-system"`.
- Add `helm_release "envoy_gateway"` in namespace `envoy-gateway-system`.
- Pin the Envoy Gateway chart version in Terraform. Do not use an unpinned chart
  version or a floating `latest`.
- Let the Envoy Gateway Helm chart install/own its own Envoy Gateway CRDs.
- Explicitly install/own Gateway API CRDs before the backend chart can render
  `Gateway` and `HTTPRoute`. Do this in infra, not backend, so backend deploy
  does not fail on missing CRDs. The implementation may use a pinned Gateway API
  CRD source or the CRDs bundled by the chosen Envoy Gateway chart, but it must
  produce these CRDs before backend deploy:
  - `gatewayclasses.gateway.networking.k8s.io`
  - `gateways.gateway.networking.k8s.io`
  - `httproutes.gateway.networking.k8s.io`
- Create a dedicated static Standard public IP for Envoy Gateway in the AKS node
  resource group. This avoids cross-resource-group Azure Load Balancer public IP
  permission problems.
- Create `EnvoyProxy` configuration to set Envoy service behavior:
  - service type `LoadBalancer`
  - Azure service annotation for public IP name
  - conservative dev resource requests/limits
- Create `GatewayClass` named for this environment, for example
  `envoy-gateway-dev`, with controller name
  `gateway.envoyproxy.io/gatewayclass-controller` and `parametersRef` to the
  `EnvoyProxy`.
- Create a temporary DNS A record for `devapi-envoy`.
- Create a certificate for `devapi-envoy.copilotgtm.com`, or include it as a
  SAN in a temporary validation certificate. Do not rely on the existing
  `devapi-tls` certificate for the temporary hostname.
- Use DNS-01 for Envoy certificates. The current `letsencrypt` ClusterIssuer
  already has Azure DNS-01 configured, but the implementation must ensure the
  Envoy validation certificate uses the DNS-01 solver and does not depend on the
  App Gateway HTTP-01 solver.
- Configure the cert-manager `Certificate` resources in namespace `copilotgtm`
  so Gateway TLS certificate refs are same-namespace.

Azure service annotations to account for:

- `service.beta.kubernetes.io/azure-pip-name`

Do not use `service.beta.kubernetes.io/azure-load-balancer-resource-group` in
the first implementation because the dedicated IP should live in the AKS node
resource group. If we later move the IP to the app resource group, add that
annotation plus explicit AKS identity permissions.

Static IP binding verification:

- The Envoy `LoadBalancer` service must show the dedicated static IP in
  `EXTERNAL-IP`.
- The Envoy service events must not show Azure load balancer reconciliation
  errors.
- The Terraform output for the Envoy IP and `kubectl get svc` external IP must
  match.
- Do not proceed to backend validation until these checks pass.

`infra/terraform/azure/dev/outputs.tf`

- Add output for the Envoy Gateway static public IP. The backend cutover and DNS
  verification should use this output instead of discovering an IP from
  `kubectl get svc`.

`infra/terraform/azure/dev/main.tf`

- Remove `module "waf"` for dev.
- Stop passing `app_gateway_id = module.waf.waf_id` into `module "aks"`; pass
  `app_gateway_id = null` so the AKS AGIC add-on is disabled.
- Remove AGIC role assignments:
  - `azurerm_role_assignment.agic_appgw_contributor`
  - `azurerm_role_assignment.agic_rg_reader`
  - `azurerm_role_assignment.agic_vnet_network_contributor`
- Remove `module.waf` from AKS `depends_on`.
- Keep the legacy `vnet_waf_subnet` input for now to avoid unrelated VNet churn.

Before the removal apply, search and account for every dev reference to:

- `module.waf`
- `module.waf.public_ip_address`
- `module.waf.waf_id`
- `module.aks.agic_identity_object_id`
- `app_gateway_id`
- `azurerm_role_assignment.agic_*`

Do not run a removal apply while any Terraform expression still depends on
`module.waf` or AGIC outputs.

`infra/terraform/azure/modules/aks/main.tf`

- No required change if dev passes `app_gateway_id = null` after the migration.
- The existing dynamic `ingress_application_gateway` block already supports
  disabling AGIC when `app_gateway_id` is null.

`infra/terraform/azure/dev/addons.tf`

- Current DNS A record points `devapi` at the Envoy Gateway public IP.
- The DNS record must reference the dedicated static Envoy public IP, not a
  discovered existing AKS load balancer IP.
- Keep the temporary `azurerm_dns_a_record` for `devapi-envoy` during the App
  Gateway removal apply to avoid the Terraform provider cycle.
- Remove the temporary `devapi-envoy` DNS record and certificate in a later
  cleanup apply.

`infra/terraform/azure/dev/variables.tf`

- Keep only the variables still needed by dev Terraform:
  - `enable_envoy_gateway`
  - `envoy_gateway_public_ip_name`
  - `envoy_gateway_class_name`
  - `envoy_validation_subdomain` until cleanup
  - `vnet_waf_subnet` until VNet cleanup
- Remove App Gateway-specific variables such as `waf_name` and `appgw_sku`.

Keep the variable surface minimal. If this is dev-only, a direct dev Terraform
change is better than a broad abstraction.

### Gateway API Policies

The old App Gateway path has `appgw.ingress.kubernetes.io/request-timeout:
"3600"`. The Envoy path must include an explicit timeout policy instead of
assuming defaults are acceptable.

Implementation requirement:

- Add `timeouts` directly to each HTTPRoute rule:
  - `rules[].timeouts.request: "3600s"`
  - `rules[].timeouts.backendRequest` may be set only if it is less than or
    equal to `request`; otherwise omit it.
- Do not rely on Envoy defaults. Envoy Gateway documentation calls out a much
  shorter default request timeout than the current App Gateway `3600` seconds.
- Do not use `BackendTrafficPolicy` for HTTP timeout in the first
  implementation unless `HTTPRoute.rules[].timeouts` is unavailable in the
  installed CRDs. If fallback is needed, document the exact
  `BackendTrafficPolicy` schema before implementation.
- If the installed Gateway API CRDs do not support `HTTPRoute.rules[].timeouts`,
  block cutover and keep App Gateway until a supported Gateway API/Envoy Gateway
  version is installed.

Validation:

- Render the HTTPRoute in Helm and confirm `timeouts.request: 3600s` is present.
- Confirm HTTPRoute status is accepted/programmed.
- Run a long-request test before DNS cutover.

### Azure DevOps / deploy pipeline

No Azure DevOps pipeline structure change should be required. Keep the dev
backend Helm step on normal upgrade/install semantics and make Gateway object
verification an explicit post-deploy check.

Deploy modes:

- `validation`: App Gateway remains primary for `devapi`; Envoy serves only
  `devapi-envoy`.
- `cutover-prepare`: Envoy serves `devapi`; App Gateway ingress remains enabled
  for rollback while DNS is moved and verified.
- `cutover-complete`: Envoy serves `devapi`; App Gateway ingress is disabled
  after DNS and live checks pass.
- `rollback`: App Gateway ingress is enabled for `devapi`; Envoy main route is
  disabled.

Deploy order for the first working version:

1. Infra pipeline apply:
   - disable AKS `ingressApplicationGateway` by passing `app_gateway_id = null`
   - destroy the dev Application Gateway
   - destroy the dev Application Gateway public IP
   - destroy AGIC role assignments
   - keep Envoy Gateway, Envoy public IP, GatewayClass, DNS-01 issuer, and
     `devapi` DNS pointed at Envoy
   - keep temporary `devapi-envoy` resources for now
2. Backend pipeline deploy:
   - use normal `helm upgrade --install`
   - run dev values in `cutover-complete`
   - render the `Gateway` listener for `devapi.copilotgtm.com`
   - render the main `HTTPRoute`
   - keep validation route disabled
   - keep legacy App Gateway ingress disabled
3. Live validation:
   - verify public DNS resolves to `20.12.200.117`
   - verify the live Gateway object has listener `https` for
     `devapi.copilotgtm.com`
   - verify the main HTTPRoute is `Accepted=True`
   - verify `https://devapi.copilotgtm.com/health`
   - verify auth, OAuth callback, webhook, and normal API flows
   - verify no 4xx/5xx spike
4. Cleanup apply after stability:
   - remove temporary `devapi-envoy` DNS/certificate/validation route resources
   - remove any remaining App Gateway-only chart values or allow rules that are
     no longer useful

## Rollout Plan

### Step 1: Remove App Gateway from dev infra

Changes:

- Disable AKS AGIC by setting `app_gateway_id = null`.
- Remove dev `module "waf"`.
- Remove AGIC role assignments.
- Keep Envoy Gateway and its public IP.
- Keep `devapi.copilotgtm.com` pointed at Envoy.
- Keep temporary `devapi-envoy` resources for a later cleanup apply.

Verify:

- Terraform plan summary should be `0 to add, 1 to change, 5 to destroy`.
- The only AKS change should be removal of the AGIC add-on block.
- Destroy list should be App Gateway, its public IP, and the three AGIC role
  assignments.

### Step 2: Force the backend Gateway API object into the desired state

Changes:

- Run the dev backend pipeline.
- Helm command uses normal upgrade/install semantics.
- Dev chart values keep Gateway API enabled and legacy ingress disabled.

Verify:

- `kubectl get gateway -n copilotgtm copilot-backend -o yaml`
  - listener name `https`
  - hostname `devapi.copilotgtm.com`
  - TLS secret `devapi-tls`
- `kubectl get httproute -n copilotgtm copilot-backend -o yaml`
  - parent ref section name `https`
  - `Accepted=True`
- `kubectl get ingress -n copilotgtm` should not show the old
  `copilot-backend` App Gateway ingress.

Provider callback validation requirement:

- Check the actual callback paths for the providers configured in this
  environment, including OAuth/Nango callbacks and HubSpot/Google/Microsoft
  webhook-style callbacks where applicable.

### Step 3: Live public validation

Verify:

- DNS resolves to Envoy public IP `20.12.200.117`.
- `curl https://devapi.copilotgtm.com/health`
- Backend pod logs receive requests.
- Gateway and HTTPRoute status conditions are accepted/programmed.
- Timeout policy status is accepted and a long request succeeds.
- No spike in 4xx/5xx.
- OAuth and webhook callbacks still hit the backend.

### Step 4: Cleanup temporary validation surface

Cleanup changes after stability:

- Remove temporary `devapi-envoy` DNS/certificate resources.
- Remove validation route values if not needed.
- Remove stale App Gateway-only comments/values from dev if they are no longer
  useful for rollback documentation.

Verify:

- No dev route depends on `azure-application-gateway`.
- No `ingressApplicationGateway` add-on remains enabled on dev AKS.
- Azure billing no longer accrues Application Gateway fixed/capacity charges
  after deletion.

## Rollback Plan

Rollback before the infra pipeline deletes App Gateway:

1. Reapply backend Helm in `rollback` mode before moving DNS if the App Gateway
   ingress was already disabled:
   - App Gateway ingress enabled
   - `ingress.className: azure-application-gateway`
   - App Gateway annotations restored
   - Gateway API main route disabled
   - temporary validation route may remain only if it does not share `devapi`
2. Confirm AGIC has reconciled the App Gateway backend/routing config.
3. Change `devapi.copilotgtm.com` DNS back to App Gateway public IP
   `20.118.21.159`.
4. Confirm `/health` and auth endpoints through App Gateway after DNS resolves
   externally.

Rollback after this App Gateway deletion is slower:

1. Re-enable or recreate the dev Application Gateway Terraform module.
2. Pass the App Gateway ID back into the AKS module to re-enable AGIC.
3. Recreate AGIC role assignments.
4. Re-render the App Gateway ingress from the backend chart.
5. Point `devapi.copilotgtm.com` DNS back to the recreated App Gateway public
   IP.

Because the current cost-saving step deletes App Gateway, the primary recovery
path after deployment should be to fix Envoy/Gateway API quickly, not to rely on
an instant DNS rollback.

## Risks And Mitigations

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Loss of App Gateway managed L7 features | Envoy Gateway is exposed directly to the internet in version 1 | Accept for dev; add Cloudflare in version 2 before using this pattern for production |
| Gateway API chart bugs | This is a new resource model for this chart | Keep old Ingress template for rollback and add focused Helm rendering checks |
| Long request timeout changes | Current App Gateway annotation uses `3600` seconds | Render `HTTPRoute.rules[].timeouts.request: 3600s`; block cutover if installed CRDs do not support it |
| TLS issuance failure | Current cert-manager HTTP-01 solver targets App Gateway | Use the Azure DNS-01 solver for Envoy certificates before DNS cutover |
| TLS secret namespace mismatch | Gateway certificate refs are namespace-sensitive | Render Gateway and HTTPRoute in `copilotgtm`, same namespace as `devapi-tls` |
| Route does not attach to Gateway | Cross-namespace parent refs and allowedRoutes can silently block attachment | Keep Gateway and HTTPRoute in `copilotgtm` for phase 1 |
| Envoy blocked by NetworkPolicy | Existing policy was written around App Gateway/VNet traffic | Add explicit `name=envoy-gateway-system` namespace allow rule on TCP 8080 |
| Missing Gateway API CRDs | Backend deploy fails before app resources are applied | Install/own Gateway API CRDs in infra before backend pipeline and verify required CRDs exist |
| Webhook provider failures | Slack/HubSpot/Google/Microsoft callbacks use `devapi` | Validate temporary route reachability, then run real provider callback checks immediately after DNS cutover |
| Terraform destroys App Gateway | Rollback becomes slow | Accept for dev cost reduction; validate Envoy immediately after backend deploy and treat App Gateway recreation as slower rollback |
| Envoy pod memory pressure | Cost work also targets smaller AKS nodes | Give Envoy conservative dev requests and check node memory before one-node downsizing |
| Stale Gateway object after Helm deploy | Reusing one Gateway object for validation and final cutover can leave live listener state stale | Use separate validation and final Gateway objects, keep normal Helm deploy semantics, and verify the live Gateway listener after deploy |
| App Gateway ingress removed before Envoy is proven | DNS points to Envoy but live Gateway may still be stale | Verify live Gateway/HTTPRoute immediately after backend deploy; if external traffic still fails, inspect Azure LB health, `externalTrafficPolicy`, and Gateway status |
| Wrong public IP used | Existing AKS LB IP may be outbound/load-balancer state, not the intended gateway frontend | Allocate and bind a dedicated static Envoy Gateway IP |
| Temporary hostname TLS mismatch | `devapi-tls` is not valid for `devapi-envoy` | Create a temporary certificate/SAN before testing HTTPS |
| Static public IP binding failure | Azure Load Balancer can reject the requested public IP if placement or identity permissions are wrong | Keep Envoy IP in the AKS node resource group and verify service external IP/events before backend validation |
| Terraform provider dependency cycle during cleanup | Removing kubectl-managed validation cert/DNS resources in the same apply as AGIC removal can cycle through the Kubernetes provider and AKS outputs | Keep temporary validation resources in the App Gateway removal apply; delete them in a later cleanup apply |
| Cost attribution overclaim | CSV rows are subscription-level | Split App Gateway cost by resource before quoting final savings |

## Success Criteria

The migration is successful when all of these are true:

- `devapi.copilotgtm.com` resolves to the Envoy Gateway public IP.
- The Envoy Gateway public IP is the dedicated static IP created for this
  migration.
- `https://devapi.copilotgtm.com/health` returns healthy response.
- Backend pod logs show traffic arriving through Envoy.
- Gateway and HTTPRoute status conditions are accepted/programmed.
- HTTPRoute renders `rules[].timeouts.request: 3600s`.
- Backend NetworkPolicy allows Envoy-to-backend traffic without relying only on
  the broad App Gateway VNet rule.
- Envoy Gateway namespace is labeled `name=envoy-gateway-system`.
- Envoy `LoadBalancer` service external IP matches the Terraform Envoy public IP
  output.
- Auth, OAuth callback, webhook, and normal API flows work.
- No unexpected 4xx/5xx increase after cutover.
- Cert-manager can issue or renew a certificate without App Gateway.
- No dev public route depends on `azure-application-gateway`.
- Dev AKS no longer has the AGIC add-on enabled.
- App Gateway resource is deleted after the stability window.
- The next billing export no longer shows Application Gateway fixed/capacity
  charges for dev.

## Changes Made So Far

Implementation has moved from Flow 1 validation to the dev cutover path. The
current code changes route `devapi.copilotgtm.com` through Envoy Gateway and
stop rendering the legacy App Gateway `Ingress`; they still do not delete the
Application Gateway infrastructure.

Terraform changes:

- Added AKS module output for the managed node resource group.
- Added dev output for the dedicated Envoy Gateway public IP.
- Added dev variables for Envoy Gateway enablement, namespace, pinned chart
  version, static IP name, GatewayClass name, and validation subdomain.
- Removed dev Application Gateway Terraform module usage.
- Disabled AKS AGIC for dev by passing `app_gateway_id = null`.
- Removed dev AGIC role assignments.
- Removed App Gateway-only dev variables `waf_name` and `appgw_sku`.
- Kept the legacy WAF subnet CIDR input temporarily to avoid unrelated VNet
  address-space churn.
- Added Terraform resources for:
  - `envoy-gateway-system` namespace with label
    `name=envoy-gateway-system`
  - dedicated Standard static public IP in the AKS node resource group
  - pinned Envoy Gateway Helm release `v1.8.1`
  - DNS-01-only `ClusterIssuer` named `letsencrypt-dns01`
  - Envoy `EnvoyProxy` using `LoadBalancer` and
    `service.beta.kubernetes.io/azure-pip-name`
  - `GatewayClass` named `envoy-gateway-dev`
  - temporary DNS A record for `devapi-envoy.copilotgtm.com`
  - temporary validation certificate `devapi-envoy-tls`
- Updated the main `devapi.copilotgtm.com` DNS record to point to the Envoy
  Gateway public IP.
- Updated the main `devapi-tls` certificate to use the DNS-01 issuer, so
  certificate renewal no longer depends on the App Gateway HTTP-01 path.
- Kept temporary `devapi-envoy` DNS/certificate resources for a later cleanup
  apply because removing them in the same apply caused a Terraform provider
  dependency cycle.

Helm changes:

- Added Gateway API values to the webservice subchart and parent chart.
- Set dev Gateway API mode to `cutover-complete`.
- Set `api.gateway.disableLegacyIngress=true` in dev so the legacy App Gateway
  `Ingress` is no longer rendered.
- Kept the dev backend Helm upgrade/install command on normal deploy semantics;
  stale Gateway API state is handled through explicit post-deploy verification.
- Added `Gateway`, main `HTTPRoute`, and validation `HTTPRoute` templates.
- Added a separate validation `Gateway` template so temporary validation traffic
  does not mutate the final Gateway object.
- Kept the existing App Gateway `Ingress` enabled by default outside explicit
  cutover mode.
- Added `api.gateway.disableLegacyIngress` so the old ingress can be disabled
  only after DNS/live validation.
- Added Envoy Gateway NetworkPolicy allowance using namespace label
  `name=envoy-gateway-system`.
- Rendered `HTTPRoute.rules[].timeouts.request: "3600s"` for Envoy routes.

Verification already run locally:

- `helm template copilot-backend charts\application -n copilotgtm -f charts\application\values.yaml -f charts\application\values-dev.yaml`
- Focused renders for Gateway, validation HTTPRoute, Ingress, and
  NetworkPolicy.
- Focused `cutover-prepare` render confirming the main `devapi` route and old
  App Gateway ingress can coexist.
- Focused `cutover-complete` render confirming the main `devapi` Gateway and
  HTTPRoute render without the old App Gateway `Ingress`.
- `terraform fmt -recursive infra\terraform\azure\dev infra\terraform\azure\modules\aks`
- `terraform validate` from `infra\terraform\azure\dev`
- `terraform plan -refresh=false` from `infra\terraform\azure\dev` after the
  App Gateway removal showed `0 to add, 1 to change, 5 to destroy`: remove the
  AKS AGIC block, destroy the dev Application Gateway, destroy the App Gateway
  public IP, and destroy the three AGIC role assignments.
- `helm lint charts\application -f charts\application\values.yaml -f charts\application\values-dev.yaml`

No live `terraform apply`, Kubernetes mutation, DNS mutation, or Azure deletion
has been run locally from this workspace for the App Gateway deletion. The dev
pipeline previously deployed the backend release manifest, but the live Gateway
object remained stale; the next backend deploy must verify the live Gateway and
HTTPRoute state explicitly.
