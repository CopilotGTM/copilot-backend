# AKS App Gateway Replacement Plan

Date: 2026-06-16
Environment: dev AKS, `copilot-gtm-aks-dev`
Decision: use Envoy Gateway + Kubernetes Gateway API, not nginx Ingress.
Goal: remove the dev Application Gateway cost by routing `devapi.copilotgtm.com`
through an open-source in-cluster gateway first, then optionally add Cloudflare
later.
Current scope: complete Flow 1 only. Do not implement Cloudflare DNS/proxy or
Cloudflare Tunnel in this phase.

Implementation stance: make the first dev migration validation-safe. The first
infra and backend deploys must create a working Envoy validation path without
removing the current App Gateway path. The main `devapi` cutover is a separate
release-window action after the validation hostname is healthy.

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

Recommended rollout:

1. Infra pipeline deploys Envoy Gateway beside the existing Application Gateway
   path.
2. Infra pipeline allocates a dedicated static public IP for Envoy in the AKS
   node resource group and binds Envoy's `LoadBalancer` service to it.
3. Backend pipeline adds a parallel `Gateway`/`HTTPRoute` for temporary
   validation hostname `devapi-envoy.copilotgtm.com`. Do not switch the current
   App Gateway ingress yet.
4. Validate health, auth-capable routes, signed webhook route reachability,
   SSE/WebSocket behavior, long requests, and TLS issuance through Envoy.
5. Prepare cutover by enabling the Envoy `devapi.copilotgtm.com` route while
   keeping the App Gateway ingress rendered.
6. Move `devapi.copilotgtm.com` DNS to the Envoy static IP.
7. Only after DNS and live checks pass, disable the old App Gateway ingress.
8. Keep App Gateway for 24-48 hours as rollback.
9. Remove AGIC/App Gateway Terraform wiring and delete App Gateway only after
   the Envoy path is stable.

## Current Verified State

Live context:

- Kubernetes context: `copilot-gtm-aks-dev`
- Helm release: `copilot-backend`, namespace `copilotgtm`, revision `517`
- Current ingress:
  - Namespace: `copilotgtm`
  - Name: `copilot-backend`
  - Class: `azure-application-gateway`
  - Host: `devapi.copilotgtm.com`
  - Address: `20.118.21.159`
  - Ports: `80, 443`
- Current Application Gateway:
  - Name: `copilot-gtm-waf-dev`
  - SKU: `Standard_v2`
  - State: `Running`
  - Public IP resource: `copilot-gtm-waf-dev-pip`
- Current AKS networking:
  - AKS network profile uses Standard Load Balancer.
  - AKS already has a Standard Load Balancer named `kubernetes` in the node
    resource group.
  - Existing AKS Load Balancer public IP: `130.131.152.31`. This must not be
    reused blindly for public API DNS. The migration should allocate a
    dedicated static Envoy Gateway IP and bind Envoy to that IP.
- Current cert-manager state:
  - `devapi-tls` certificate is ready for `devapi.copilotgtm.com`.
  - Current `ClusterIssuer` includes an HTTP-01 solver tied to
    `azure-application-gateway` plus Azure DNS-01 support.

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
5. Add a temporary Gateway API route for `devapi-envoy.copilotgtm.com` without
   removing the existing App Gateway ingress for `devapi.copilotgtm.com`.
6. Use DNS-01 for the Envoy validation certificate so TLS can be tested before
   public traffic is moved from App Gateway.
7. Change Azure DNS `devapi` A record from `module.waf.public_ip_address` to the
   dedicated Envoy public IP only after the temporary host passes.
8. Enable the main Envoy Gateway API route for `devapi.copilotgtm.com` before
   DNS cutover, while keeping the old App Gateway `Ingress` rendered.
9. Disable the old App Gateway `Ingress` only after DNS resolves externally to
   the Envoy IP and live checks pass.
10. After validation, remove the AKS AGIC add-on connection and App Gateway
   Terraform module usage from dev.

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

- Keep the current App Gateway ingress values for rollback during the first
  deploy.
- Add a new Gateway API values block. Suggested shape:
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
- During validation, keep:
  - App Gateway `Ingress` for `devapi.copilotgtm.com`
  - Gateway API `Gateway`/`HTTPRoute` for `devapi-envoy.copilotgtm.com`
- During cutover phase 1, enable Gateway API for `devapi.copilotgtm.com` but
  keep the old App Gateway ingress enabled.
- During cutover phase 2, after DNS and live checks pass, set
  `api.gateway.disableLegacyIngress=true` for dev.

`charts/application/charts/webservice/templates/ingress.yaml`

- Keep this template for rollback and prod until each environment is migrated.
- Do not delete it in phase 1.

New chart templates to add under `charts/application/charts/webservice/templates/`:

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
- `httproute-validation.yaml` or a values-driven second route
  - Same backend service.
  - Hostname `devapi-envoy.copilotgtm.com`.
  - Used only during validation.

Important sequencing constraint:

- Do not disable the existing `copilot-backend` App Gateway ingress until the
  Envoy route has served `devapi` successfully after DNS cutover. If the backend
  pipeline removes the App Gateway ingress while DNS still points to App
  Gateway, App Gateway will stop receiving config for `devapi` and traffic can
  break.
- For validation, create a separate temporary Gateway/HTTPRoute for
  `devapi-envoy.copilotgtm.com`.
- For cutover, create the main Envoy `devapi` route first, keep App Gateway
  ingress rendered, move DNS, verify, and only then disable the App Gateway
  ingress in a follow-up backend deploy.
- After the cutover observation window, remove the temporary validation host to
  keep the public surface small.

`charts/application/charts/webservice/templates/networkpolicy.yaml`

- Add a values-driven allow rule for Envoy Gateway traffic to API port `8080`.
- Required implementation:
  - label the Envoy namespace from Terraform with
    `name: envoy-gateway-system`
  - add `api.networkPolicy.envoyGateway.enabled`
  - add `api.networkPolicy.envoyGateway.namespaceSelector.matchLabels.name`
    defaulting to `envoy-gateway-system`
  - allow TCP `8080`
- Keep existing App Gateway/VNet allowance until App Gateway is deleted.

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

- Do not remove App Gateway in the first apply.
- After Envoy path is validated:
  - Remove `module "waf"` for dev or guard it with a variable.
  - Stop passing `app_gateway_id = module.waf.waf_id` into `module "aks"`.
  - Remove AGIC role assignments:
    - `azurerm_role_assignment.agic_appgw_contributor`
    - `azurerm_role_assignment.agic_rg_reader`
    - `azurerm_role_assignment.agic_vnet_network_contributor`
  - Remove `module.waf` from AKS `depends_on`.

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

- Current DNS A record points `devapi` at `module.waf.public_ip_address`.
- Change it to the Envoy Gateway public IP only after the temporary validation
  hostname has passed tests.
- The DNS record must reference the dedicated static Envoy public IP, not a
  discovered existing AKS load balancer IP.
- Add a temporary `azurerm_dns_a_record` for `devapi-envoy` during validation.
- Remove the temporary `devapi-envoy` DNS record after cutover unless a short
  observation window is explicitly desired.

`infra/terraform/azure/dev/variables.tf`

- Add variables only if needed:
  - `enable_app_gateway`
  - `enable_envoy_gateway`
  - `envoy_gateway_public_ip_name`
  - `envoy_gateway_class_name`
  - `envoy_validation_subdomain`

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

No Azure DevOps pipeline structure change should be required if the infra and
backend changes are split correctly.

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

1. Infra pipeline apply 1:
   - create Envoy static public IP
   - install Gateway API CRDs
   - install Envoy Gateway
   - create `EnvoyProxy`
   - create `GatewayClass`
   - create temporary `devapi-envoy` DNS
   - create/verify temporary TLS certificate path
   - keep App Gateway and `devapi` DNS unchanged
2. Backend pipeline deploy 1:
   - keep current App Gateway ingress for `devapi`
   - set Gateway API mode to `validation`
   - add temporary Gateway/HTTPRoute validation path for `devapi-envoy`
   - include NetworkPolicy allowance for Envoy-to-backend traffic
3. Manual/live validation:
   - test temporary Envoy hostname
   - test TLS issuance/renewal path
   - test NetworkPolicy/backend reachability
   - test long request timeout behavior
4. Backend pipeline deploy 2:
   - set Gateway API mode to `cutover-prepare`
   - enable the main `devapi` Envoy route
   - keep the old App Gateway ingress rendered
   - verify both the App Gateway path and Envoy path still have valid routes
5. Infra pipeline apply 2 during the cutover window:
   - change `devapi` DNS to Envoy static IP
   - keep App Gateway resource and AGIC wiring in place
6. Live cutover validation:
   - verify public DNS resolves to the Envoy IP after TTL
   - verify `https://devapi.copilotgtm.com/health`
   - verify auth, OAuth callback, webhook, and normal API flows
   - verify no 4xx/5xx spike
7. Backend pipeline deploy 3:
   - set Gateway API mode to `cutover-complete`
   - disable old dev App Gateway ingress only after live validation passes
8. Infra pipeline apply 3 after 24-48 hours:
   - remove AGIC/App Gateway dependencies
   - delete App Gateway

## Rollout Plan

### Step 1: Prepare Envoy Gateway without touching production traffic

Changes:

- Add the Envoy Gateway Helm release.
- Create a dedicated static public IP for Envoy Gateway.
- Bind Envoy managed service to that static IP.
- Add temporary DNS and certificate resources for
  `devapi-envoy.copilotgtm.com`.
- Keep App Gateway and current `devapi` DNS unchanged.

Verify:

- `kubectl get pods -n envoy-gateway-system`
- `kubectl get gatewayclass`
- `kubectl get envoyproxy -A`
- `kubectl get crd gateways.gateway.networking.k8s.io`
- `kubectl get crd httproutes.gateway.networking.k8s.io`
- `kubectl get svc -A` and confirm Envoy service has the expected dedicated
  external IP.
- `kubectl get certificate -A` shows the temporary certificate as `Ready=True`.
- Current `devapi.copilotgtm.com` traffic still works through App Gateway.

### Step 2: Add a temporary validation hostname

Changes:

- Add a temporary DNS record such as `devapi-envoy.copilotgtm.com`.
- Route it to the Envoy public IP.
- Add a separate temporary `Gateway`/`HTTPRoute` path. Keep the existing App
  Gateway ingress for `devapi.copilotgtm.com` untouched during validation.

Verify:

- `curl https://devapi-envoy.copilotgtm.com/health`
- Login/auth-capable routes.
- API routes that do not depend on third-party callback allowlists.
- Webhook route reachability with signed/local test requests where possible.
- SSE/WebSocket behavior if the frontend uses it.
- Long-running request behavior because current App Gateway timeout is `3600`.
- Certificate issuance and renewal path through DNS-01.
- Backend reachability through NetworkPolicy.
- Gateway/HTTPRoute/policy status conditions are accepted/programmed.

Limit of this step: OAuth and third-party webhooks are still configured to use
`devapi.copilotgtm.com`, so the temporary hostname proves Envoy routing/TLS but
does not fully prove provider callbacks unless provider allowlists are updated.

Provider callback validation requirement:

- During temporary-host validation, test signed/local webhook requests where the
  provider cannot call the temporary hostname.
- During real `devapi` cutover, check the actual callback paths for the
  providers configured in this environment, including OAuth/Nango callbacks and
  HubSpot/Google/Microsoft webhook-style callbacks where applicable.
- Keep App Gateway rollback available until these real callback checks pass.

### Step 3: Switch `devapi` DNS

Changes:

- Enable Gateway API route for the main hostname first.
- Keep the old dev App Gateway ingress rendered.
- Point `devapi.copilotgtm.com` to the Envoy Gateway public IP.
- Remove the temporary validation route or leave it only for a short
  post-cutover observation window.
- Keep App Gateway running for rollback.

Verify:

- DNS resolves to Envoy public IP.
- `curl https://devapi.copilotgtm.com/health`
- Backend pod logs receive requests.
- Gateway and HTTPRoute status conditions are accepted/programmed.
- Timeout policy status is accepted and a long request succeeds.
- No spike in 4xx/5xx.
- OAuth and webhook callbacks still hit the backend.
- DNS TTL has elapsed and external resolution returns the dedicated Envoy IP.
- App Gateway ingress still exists until these checks pass.

### Step 3.5: Disable legacy App Gateway ingress

Changes:

- Disable the old dev App Gateway ingress in the backend chart only after Step 3
  passes.
- Keep App Gateway infrastructure running for 24-48 hours as a fallback while
  Envoy serves live `devapi` traffic.

Verify:

- No live request path depends on the App Gateway ingress.
- Envoy still serves `devapi.copilotgtm.com`.
- Rollback mode can still re-render the App Gateway ingress if DNS must move
  back to `20.118.21.159`.

### Step 4: Remove App Gateway dependency

Changes:

- Remove AGIC/App Gateway Terraform wiring from dev.
- Remove App Gateway-specific Helm annotations from the dev path.
- Delete App Gateway only after the Envoy path has been stable for 24-48 hours.

Verify:

- No dev route depends on `azure-application-gateway`.
- No `ingressApplicationGateway` add-on remains enabled on dev AKS.
- Azure billing no longer accrues Application Gateway fixed/capacity charges
  after deletion.

## Rollback Plan

Rollback before App Gateway deletion:

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

Rollback is only fast if App Gateway still exists and the backend chart can
restore the App Gateway ingress. It is faster if the App Gateway ingress was
never disabled during `cutover-prepare`. Do not delete App Gateway in the same
pipeline run as the DNS/backend cutover.

Rollback after App Gateway deletion is slower because the App Gateway resource
must be recreated. That is why deletion should happen only after the 24-48 hour
stability window.

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
| Terraform destroys App Gateway too early | Rollback becomes slow | Split rollout into two applies: add Envoy first, remove App Gateway later |
| Envoy pod memory pressure | Cost work also targets smaller AKS nodes | Give Envoy conservative dev requests and check node memory before one-node downsizing |
| App Gateway ingress removed before DNS is proven | DNS may still point to App Gateway while its route config has been removed | Use `cutover-prepare`: enable Envoy main route, keep App Gateway ingress, move DNS, verify, then disable legacy ingress |
| Wrong public IP used | Existing AKS LB IP may be outbound/load-balancer state, not the intended gateway frontend | Allocate and bind a dedicated static Envoy Gateway IP |
| Temporary hostname TLS mismatch | `devapi-tls` is not valid for `devapi-envoy` | Create a temporary certificate/SAN before testing HTTPS |
| Static public IP binding failure | Azure Load Balancer can reject the requested public IP if placement or identity permissions are wrong | Keep Envoy IP in the AKS node resource group and verify service external IP/events before backend validation |
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

Implementation has started for Flow 1 dev validation. The current code changes
prepare Envoy beside the existing Application Gateway path; they do not delete
or disable App Gateway.

Terraform changes:

- Added AKS module output for the managed node resource group.
- Added dev output for the dedicated Envoy Gateway public IP.
- Added dev variables for Envoy Gateway enablement, namespace, pinned chart
  version, static IP name, GatewayClass name, and validation subdomain.
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

Helm changes:

- Added Gateway API values to the webservice subchart and parent chart.
- Set dev Gateway API mode to `validation`.
- Added `Gateway`, main `HTTPRoute`, and validation `HTTPRoute` templates.
- Kept the existing App Gateway `Ingress` enabled by default.
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
- `terraform fmt -recursive infra\terraform\azure\dev infra\terraform\azure\modules\aks`
- `terraform validate` from `infra\terraform\azure\dev`

No live `terraform apply`, `helm upgrade`, Kubernetes mutation, DNS mutation, or
Azure deletion has been run yet.
