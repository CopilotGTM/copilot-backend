# Kai Google Chat Bot Creation Plan

This document lists the complete plan for creating Kai as a Google Chat bot across `local`, `dev`, and `prod`.

Google Chat is different from Slack and Microsoft Teams in this repo:

- Slack is mostly manifest-driven.
- Teams uses Azure Bot Service plus an app package.
- Google Chat is a combination of Google Cloud project resources, Google Chat API configuration, a backend HTTPS endpoint, and Google Workspace Marketplace distribution.

The goal is to make the Google Chat setup repeatable like the existing `teams_bot` and `azure_openai` Terraform module patterns, while recognizing that some Google Chat and Marketplace settings still require Google Console configuration.

---

## 1. Environments

Create and maintain one Google Cloud project per environment.

| Environment | GCP project name | GCP project ID | Runtime endpoint |
|---|---|---|---|
| `local` | Copilot GTM Local | `copilotgtm-local` | `https://{developer-ngrok-host}.ngrok-free.dev/users/bots/google-chat/events` |
| `dev` | CopilotGTM Dev | `copilotgtm-dev` | `https://devapi.copilotgtm.com/users/bots/google-chat/events` |
| `prod` | Copilot GTM | `copilotgtm` | `https://api.copilotgtm.com/users/bots/google-chat/events` |

Each environment must have its own:

- Google Cloud project.
- Google Chat app configuration.
- Google service account.
- Marketplace SDK configuration.
- App ID.
- Backend config values.

---

## 2. Services To Create Or Enable

### 2.1 Google Cloud APIs

Enable these APIs in each GCP project.

| API service ID | Required for |
|---|---|
| `chat.googleapis.com` | Google Chat app registration and Chat REST API calls. |
| `appsmarket-component.googleapis.com` | Google Workspace Marketplace SDK configuration and publication. |
| `iam.googleapis.com` | Service accounts, IAM bindings, workload identity pools. |
| `iamcredentials.googleapis.com` | Service account impersonation and short-lived token generation. |
| `sts.googleapis.com` | Workload Identity Federation token exchange. |
| `logging.googleapis.com` | Google Chat app error logging in Cloud Logging. |

Optional later:

| API service ID | When needed |
|---|---|
| `admin.googleapis.com` | Only if Kai needs to programmatically inspect Workspace users, groups, or domain-level configuration. Not required for MVP. |

### 2.2 Google Cloud IAM Resources

Create one service account per environment.

| Environment | Service account |
|---|---|
| `local` | `kai-chat-bot-local@copilotgtm-local.iam.gserviceaccount.com` |
| `dev` | `kai-chat-bot-dev@copilotgtm-dev.iam.gserviceaccount.com` |
| `prod` | `kai-chat-bot-prod@copilotgtm.iam.gserviceaccount.com` |

Purpose:

- Call the Google Chat REST API.
- Send proactive messages after long-running async work.
- Send follow-up messages when LLM processing takes longer than the synchronous response window.
- Represent Kai as the bot author in Chat spaces.

Do not create JSON service account keys. The organization policy `iam.disableServiceAccountKeyCreation` blocks key creation, and the production plan should remain keyless.

### 2.3 Local Credential Resources

For local development, use `gcloud` Application Default Credentials with service account impersonation.

Create this IAM binding on the local service account:

| Principal | Role | Resource |
|---|---|---|
| `tech@copilotgtm.com` | `roles/iam.serviceAccountTokenCreator` | `kai-chat-bot-local` service account |

Developer setup:

```powershell
gcloud auth login
gcloud config set project copilotgtm-local
gcloud auth application-default login
gcloud config set auth/impersonate_service_account kai-chat-bot-local@copilotgtm-local.iam.gserviceaccount.com
gcloud auth application-default print-access-token
```

Expected result:

- Last command prints an access token.
- Backend can use `google.auth.default()` with no service account key file.
- No `GOOGLE_APPLICATION_CREDENTIALS` is needed for local unless the backend explicitly requires one.

### 2.4 Dev And Prod Credential Resources

For AKS-hosted environments, create Google Workload Identity Federation.

Resources per environment:

| Resource | Name |
|---|---|
| Workload Identity Pool | `copilot-aks-pool` |
| Workload Identity Provider | `azure` |
| Target service account | `kai-chat-bot-{env}@{project-id}.iam.gserviceaccount.com` |
| IAM grant | `roles/iam.serviceAccountTokenCreator` to the Azure workload identity principal |

Trust model:

```text
AKS backend pod
-> Azure Workload Identity token
-> GCP Security Token Service
-> GCP service account impersonation
-> Google Chat REST API
```

The backend pod must mount a credential config file:

```text
/var/run/gcp/credential-config.json
```

And set:

```text
GOOGLE_APPLICATION_CREDENTIALS=/var/run/gcp/credential-config.json
```

The credential config file contains identifiers, not secrets.

---

## 3. Google Chat App Configuration

Create one Google Chat app per environment in:

```text
Google Cloud Console
-> APIs & Services
-> Google Chat API
-> Configuration
```

Use the corresponding file as the source of truth:

| Environment | Config file |
|---|---|
| `local` | `google-chat-app-manifest/local/chat-app-config.json` |
| `dev` | `google-chat-app-manifest/dev/chat-app-config.json` |
| `prod` | `google-chat-app-manifest/prod/chat-app-config.json` |

### 3.1 App Info

| Environment | App name | Description |
|---|---|---|
| `local` | `Kai - LOCAL` | AI sales assistant for local testing. |
| `dev` | `Kai - DEV` | AI sales assistant for dev testing. |
| `prod` | `Kai` | AI sales assistant for real users. |

Use a public HTTPS avatar URL:

```text
https://www.copilotgtm.com/assets/kai-avatar.png
```

### 3.2 Functionality

Enable:

- Receive 1:1 messages.
- Join spaces and group conversations.

This is required because Kai must support:

- Direct messages.
- Space mentions.
- Space membership events.
- Slash commands.
- Card interactions.

### 3.3 Connection Settings

Use HTTP endpoint mode.

| Environment | App URL | Authentication audience |
|---|---|---|
| `local` | `https://{developer-ngrok-host}.ngrok-free.dev/users/bots/google-chat/events` | Same as App URL |
| `dev` | `https://devapi.copilotgtm.com/users/bots/google-chat/events` | Same as App URL |
| `prod` | `https://api.copilotgtm.com/users/bots/google-chat/events` | Same as App URL |

Do not use:

- Apps Script.
- Dialogflow.
- Pub/Sub.

Reason: HTTP endpoint mode keeps Google Chat aligned with the existing Slack and Teams backend webhook architecture.

### 3.4 Slash Commands

Create these slash commands.

| ID | Command | Description | Dialog |
|---|---|---|---|
| `1` | `/help` | Get help using Kai | No |
| `2` | `/login` | Link your CopilotGTM account | Yes |

### 3.5 Visibility

| Environment | Visibility |
|---|---|
| `local` | Specific people and groups, usually the developer email only. |
| `dev` | Specific people and groups, usually `dev-team@copilotgtm.com` or the internal domain. |
| `prod` | Driven by the public Workspace Marketplace listing. |

After saving the Chat API configuration, copy the generated App ID and write it back into the environment's `chat-app-config.json`.

---

## 4. Backend Service Requirements

The backend must expose:

```text
POST /users/bots/google-chat/events
```

### 4.1 Request Verification

Google Chat sends an `Authorization: Bearer <JWT>` header.

The backend must verify:

| Check | Required value |
|---|---|
| Signature | Valid signature from Google's Chat service account certificate/JWKS. |
| Issuer | `chat@system.gserviceaccount.com` |
| Audience | Exact configured endpoint URL for the environment. |

Reject requests that fail validation.

There is no Slack-style signing secret for Google Chat.

### 4.2 Event Handling

Handle these Google Chat event types.

| Event type | Meaning | Required behavior |
|---|---|---|
| `ADDED_TO_SPACE` | Kai was added to a DM or space. | Persist installation and return welcome message/card. |
| `REMOVED_FROM_SPACE` | Kai was removed. | Mark installation inactive/uninstalled. |
| `MESSAGE` | User sent a DM or mentioned Kai. | Route to existing agent/chat pipeline. |
| `CARD_CLICKED` | User clicked a card action. | Route to card action handler. |
| `SLASH_COMMAND` | User used `/help`, `/login`, etc. | Route by command ID. |
| `APP_HOME` | User opened the app home surface. | Return the app home card if supported. |

### 4.3 Response Strategy

Use two response paths:

| Scenario | Response path |
|---|---|
| Fast response under synchronous window | Return a Google Chat `Message` JSON response directly from the event endpoint. |
| Long LLM/tool response | Acknowledge quickly, process async, then call Google Chat REST API with the service account. |

Async send endpoint:

```text
POST https://chat.googleapis.com/v1/{space}/messages
```

Required OAuth scope:

```text
https://www.googleapis.com/auth/chat.bot
```

### 4.4 Data Model

Reuse the existing bot platform installation model if available in backend.

Expected stored values:

| Field | Source |
|---|---|
| Platform | `google_chat` |
| External space ID | `event.space.name` |
| External user ID | `event.user.name` |
| Install status | `ADDED_TO_SPACE` / `REMOVED_FROM_SPACE` |
| App ID | Google Chat App ID for the environment |

No new database table should be added unless the backend implementation finds a missing Google-specific field that cannot fit the existing install model.

---

## 5. Workspace Marketplace Setup

Google Chat apps are distributed through the Google Workspace Marketplace flow, even for private/internal apps.

Configure:

```text
Google Cloud Console
-> APIs & Services
-> Google Workspace Marketplace SDK
-> App Configuration
```

### 5.1 Marketplace App Configuration

| Field | Local | Dev | Prod |
|---|---|---|---|
| Visibility | Private | Private | Public |
| Installation settings | Individual only | Individual + admin | Individual + admin |
| Integration | Google Chat app | Google Chat app | Google Chat app |
| OAuth scopes | `chat.bot` | `chat.bot` | `chat.bot` |
| Developer name | CopilotGTM local/dev label | CopilotGTM | CopilotGTM |
| Support email | Developer email | `support@copilotgtm.com` | `support@copilotgtm.com` |

Scope:

```text
https://www.googleapis.com/auth/chat.bot
```

Avoid adding read scopes for MVP. Sensitive or restricted scopes can trigger admin approval or OAuth verification.

### 5.2 Store Listing Assets

Required especially for prod:

| Asset | Requirement |
|---|---|
| Icon | 128x128 PNG |
| Banner | Marketplace-compatible banner image |
| Screenshots | DM, space mention, card/action examples |
| Short description | One-line description |
| Detailed description | What Kai does and what data it uses |
| Category | Productivity |
| Language | English |
| Privacy URL | `https://www.copilotgtm.com/privacy` |
| Terms URL | `https://www.copilotgtm.com/terms` |

### 5.3 Publication

| Environment | Publication action |
|---|---|
| `local` | Publish privately or keep draft if only testing configuration. |
| `dev` | Publish privately to internal users. |
| `prod` | Submit public listing for Google review. |

Important: once a Marketplace app is saved as public or private, changing that visibility later is constrained. Treat prod visibility as a launch decision.

---

## 6. Terraform Plan

Create a Terraform module:

```text
terraform/azure/modules/google_chat/
  main.tf
  variables.tf
  outputs.tf
```

Even though the module lives under `terraform/azure`, it should manage the Google-side resources needed by the Azure-hosted backend. This follows the existing repo convention where one environment root wires all required infrastructure for that environment.

### 6.1 Terraform Provider Requirements

Add Google provider configuration to environment roots that use the module.

Expected providers:

```hcl
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}
```

Provider inputs:

```hcl
provider "google" {
  project = var.google_chat_project_id
}
```

The exact authentication method depends on where Terraform is executed:

- Local developer apply: `gcloud` ADC.
- CI apply: Azure DevOps OIDC or another approved GCP federation path.

### 6.2 Module Inputs

Recommended variables:

```hcl
variable "env" {
  type = string
}

variable "project_id" {
  type = string
}

variable "project_number" {
  type = string
}

variable "service_account_id" {
  type = string
}

variable "service_account_display_name" {
  type = string
}

variable "enable_workload_identity_federation" {
  type    = bool
  default = false
}

variable "azure_tenant_id" {
  type    = string
  default = null
}

variable "azure_backend_object_id" {
  type    = string
  default = null
}

variable "chat_app_id" {
  type    = string
  default = ""
}

variable "chat_auth_audience" {
  type = string
}

variable "key_vault_id" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
```

### 6.3 Module Resources

Create these in `main.tf`.

```hcl
resource "google_project_service" "chat" {
  service = "chat.googleapis.com"
}

resource "google_project_service" "marketplace" {
  service = "appsmarket-component.googleapis.com"
}

resource "google_project_service" "iam" {
  service = "iam.googleapis.com"
}

resource "google_project_service" "iamcredentials" {
  service = "iamcredentials.googleapis.com"
}

resource "google_project_service" "sts" {
  service = "sts.googleapis.com"
}

resource "google_project_service" "logging" {
  service = "logging.googleapis.com"
}

resource "google_service_account" "chat_bot" {
  account_id   = var.service_account_id
  display_name = var.service_account_display_name
}
```

For dev/prod only:

```hcl
resource "google_iam_workload_identity_pool" "aks" {
  count                     = var.enable_workload_identity_federation ? 1 : 0
  workload_identity_pool_id = "copilot-aks-pool"
  display_name              = "Copilot AKS Pool"
}

resource "google_iam_workload_identity_pool_provider" "azure" {
  count                              = var.enable_workload_identity_federation ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.aks[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "azure"
  display_name                       = "Azure AKS Provider"

  attribute_mapping = {
    "google.subject"      = "assertion.sub"
    "attribute.azure_oid" = "assertion.oid"
  }

  attribute_condition = "assertion.oid == '${var.azure_backend_object_id}'"

  oidc {
    issuer_uri = "https://sts.windows.net/${var.azure_tenant_id}/"
  }
}
```

Grant impersonation:

```hcl
resource "google_service_account_iam_member" "backend_impersonation" {
  count              = var.enable_workload_identity_federation ? 1 : 0
  service_account_id = google_service_account.chat_bot.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "principal://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/copilot-aks-pool/subject/${var.azure_backend_object_id}"
}
```

For local only, either:

- Manage the `tech@copilotgtm.com` impersonation binding through Terraform.
- Or document it as a manual console grant.

Terraform-managed local grant:

```hcl
resource "google_service_account_iam_member" "local_human_impersonation" {
  count              = var.env == "local" ? 1 : 0
  service_account_id = google_service_account.chat_bot.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:tech@copilotgtm.com"
}
```

### 6.4 Azure Key Vault Secrets

Store non-key configuration in Azure Key Vault.

| Secret name | Value |
|---|---|
| `google-chat-project-id` | GCP project ID |
| `google-chat-project-number` | GCP project number |
| `google-chat-service-account-email` | Service account email |
| `google-chat-app-id` | Chat App ID after Google Chat config save |
| `google-chat-auth-audience` | Exact endpoint audience URL |
| `google-chat-credential-config-json` | Dev/prod WIF credential config JSON, if mounted from Key Vault |

Do not store `google-chat-service-account-json`; no JSON keys should exist.

### 6.5 Module Outputs

Recommended outputs:

```hcl
output "service_account_email" {
  value = google_service_account.chat_bot.email
}

output "service_account_name" {
  value = google_service_account.chat_bot.name
}

output "project_id" {
  value = var.project_id
}

output "project_number" {
  value = var.project_number
}

output "workload_identity_pool_name" {
  value = try(google_iam_workload_identity_pool.aks[0].name, null)
}

output "credential_config_path" {
  value = var.enable_workload_identity_federation ? "/var/run/gcp/credential-config.json" : null
}
```

### 6.6 Environment Wiring

Add the module to:

```text
terraform/azure/local/main.tf
terraform/azure/dev/main.tf
terraform/azure/prod/main.tf
```

Local:

```hcl
module "google_chat" {
  source = "../modules/google_chat"

  env                          = var.env
  project_id                   = var.google_chat_project_id
  project_number               = var.google_chat_project_number
  service_account_id           = "kai-chat-bot-local"
  service_account_display_name = "Kai Chat Bot Local"
  chat_app_id                  = var.google_chat_app_id
  chat_auth_audience           = var.google_chat_auth_audience
  key_vault_id                 = azurerm_key_vault.master_key_vault.id

  enable_workload_identity_federation = false
}
```

Dev/prod:

```hcl
module "google_chat" {
  source = "../modules/google_chat"

  env                          = var.env
  project_id                   = var.google_chat_project_id
  project_number               = var.google_chat_project_number
  service_account_id           = "kai-chat-bot-${var.env}"
  service_account_display_name = "Kai Chat Bot ${title(var.env)}"
  chat_app_id                  = var.google_chat_app_id
  chat_auth_audience           = var.google_chat_auth_audience
  key_vault_id                 = module.master_key_vault.key_vault_id

  enable_workload_identity_federation = true
  azure_tenant_id                     = data.azuread_client_config.current.tenant_id
  azure_backend_object_id             = module.workload_identity_backend.object_id
}
```

Add variables to each env's `variables.tf`:

```hcl
variable "google_chat_project_id" {
  type = string
}

variable "google_chat_project_number" {
  type = string
}

variable "google_chat_app_id" {
  type    = string
  default = ""
}

variable "google_chat_auth_audience" {
  type = string
}
```

Add values to `terraform.tfvars` or Azure DevOps `TF_VAR_*` variables.

---

## 7. Azure DevOps And Runtime Config

Add these variables to the backend deployment variable group.

| Variable | Local | Dev | Prod |
|---|---|---|---|
| `GOOGLE_CHAT_PROJECT_ID` | `copilotgtm-local` | dev project ID | `copilotgtm` |
| `GOOGLE_CHAT_PROJECT_NUMBER` | local project number | dev project number | prod project number |
| `GOOGLE_CHAT_APP_ID` | local App ID | dev App ID | prod App ID |
| `GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL` | local SA email | dev SA email | prod SA email |
| `GOOGLE_CHAT_AUTH_AUDIENCE` | ngrok endpoint | dev endpoint | prod endpoint |
| `GOOGLE_APPLICATION_CREDENTIALS` | unset | `/var/run/gcp/credential-config.json` | `/var/run/gcp/credential-config.json` |

Backend code should use:

```python
import google.auth

credentials, project_id = google.auth.default(
    scopes=["https://www.googleapis.com/auth/chat.bot"],
)
```

---

## 8. Execution Order

### Phase 1: Local Bot

1. Confirm `gcloud` works locally.
2. Create or select GCP project `copilotgtm-local`.
3. Enable required APIs.
4. Create `kai-chat-bot-local` service account.
5. Grant `tech@copilotgtm.com` `roles/iam.serviceAccountTokenCreator` on the service account.
6. Run ADC impersonation commands.
7. Verify `gcloud auth application-default print-access-token`.
8. Start ngrok against the local backend port.
9. Replace local config placeholders:
   - `{{DEVELOPER_NGROK_HOST}}`
   - `{{DEVELOPER_EMAIL}}`
10. Fill Google Chat API configuration page.
11. Save and record App ID.
12. Implement or deploy backend endpoint locally.
13. Test:
   - Add Kai to DM.
   - Send `/help`.
   - Send a normal message.
   - Confirm request verification.
   - Confirm async send works.

### Phase 2: Terraform Module

1. Add `terraform/azure/modules/google_chat`.
2. Add Google provider configuration.
3. Add variables and outputs.
4. Wire local, dev, and prod env roots.
5. Store non-secret config in Key Vault.
6. Run `terraform fmt`.
7. Run `terraform validate` for changed roots.
8. Apply local/dev first.

### Phase 3: Dev Bot

1. Confirm dev GCP project ID and project number.
2. Apply Terraform module for dev.
3. Confirm service account exists.
4. Confirm Workload Identity Pool and Azure provider exist.
5. Confirm backend Azure Workload Identity object ID is trusted.
6. Mount credential config into backend pod.
7. Set `GOOGLE_APPLICATION_CREDENTIALS`.
8. Configure Google Chat API page using `dev/chat-app-config.json`.
9. Save and record dev App ID.
10. Configure private Marketplace listing.
11. Deploy backend with Google Chat endpoint enabled.
12. Test internal install and DM.

### Phase 4: Prod Bot

1. Apply Terraform module for prod.
2. Confirm prod service account and WIF resources.
3. Configure prod Google Chat API page.
4. Save and record prod App ID.
5. Configure public Marketplace listing.
6. Add production assets:
   - icon
   - banner
   - screenshots
   - privacy URL
   - terms URL
   - support email
7. Submit for Google review.
8. Deploy backend production support.
9. Test with internal Workspace domain first.
10. After approval, test external Workspace installation.

---

## 9. Validation Checklist

Use this checklist per environment.

- [ ] GCP project exists.
- [ ] Project ID recorded.
- [ ] Project number recorded.
- [ ] Required APIs enabled.
- [ ] Service account exists.
- [ ] No service account JSON key exists.
- [ ] Local ADC impersonation works or dev/prod WIF works.
- [ ] Google Chat API configuration saved.
- [ ] App ID recorded.
- [ ] Backend endpoint deployed.
- [ ] JWT issuer validation works.
- [ ] JWT audience validation works.
- [ ] `ADDED_TO_SPACE` event persists installation.
- [ ] `REMOVED_FROM_SPACE` event marks uninstall.
- [ ] `/help` works.
- [ ] `/login` works or returns expected link flow.
- [ ] Normal DM message reaches the agent pipeline.
- [ ] Async response posts back to Google Chat.
- [ ] Marketplace SDK app configuration complete.
- [ ] Private or public publication complete.
- [ ] Non-developer user can install and message Kai.

---

## 10. Ownership Boundaries

### Terraform Should Own

- Required Google APIs.
- Service account.
- IAM impersonation bindings.
- Workload Identity Federation pool/provider for dev/prod.
- Azure Key Vault config secrets.
- Azure DevOps variable group values where practical.

### Google Console Still Owns

- Google Chat API app configuration.
- Chat App ID generation.
- Marketplace listing content and publication.
- Public Marketplace review.

### Backend Should Own

- `/users/bots/google-chat/events`.
- Google Chat JWT verification.
- Event dispatch.
- Installation persistence.
- Agent pipeline handoff.
- Synchronous and async message responses.

### Product/Marketing Should Own

- Production Marketplace copy.
- Screenshots.
- Icon/banner assets.
- Privacy and terms review.
- Support contact readiness.

---

## 11. Open Inputs Needed

Before dev/prod can be fully wired:

| Input | Needed for |
|---|---|
| Dev GCP project ID | Dev Terraform and Chat API config |
| Dev GCP project number | Dev WIF principal path |
| Prod GCP project number | Prod WIF principal path |
| Backend Azure Workload Identity object IDs | WIF attribute condition and impersonation grant |
| Final prod Marketplace visibility decision | Public review versus private internal app |
| Production assets | Marketplace submission |

---

## 12. References

- Google Chat overview: https://developers.google.com/workspace/chat
- Google Chat authentication and authorization: https://developers.google.com/workspace/chat/authenticate-authorize-chat-app
- Receive and respond to Google Chat interactions: https://developers.google.com/workspace/chat/receive-respond-interactions
- Enable Google Workspace APIs: https://developers.google.com/workspace/guides/enable-apis
- Configure Google Workspace Marketplace SDK: https://developers.google.com/workspace/marketplace/enable-configure-sdk
- Google Workspace Marketplace publishing: https://developers.google.com/workspace/marketplace/how-to-publish
