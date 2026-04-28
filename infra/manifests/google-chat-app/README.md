# Google Chat App Manifest (Kai for Google Chat)

Unlike Slack (single `manifest.yaml`) or Microsoft Teams (single `manifest.json` zipped as an app package), **Google Chat does not have a single uploadable manifest file**. An app's configuration is entered into the **Google Chat API configuration page inside Google Cloud Console**, and publication/installation is handled by the **Google Workspace Marketplace SDK**.

This folder therefore contains:

- `dev/`, `local/`, `prod/` — per-environment reference config files (`chat-app-config.json`) that describe every value to paste into the Cloud Console when registering that environment's Chat app. Treat them as the source of truth for what goes in the console.
- This `README.md` — the end-to-end process for creating, configuring, deploying, and distributing the Google Chat app so users in any Workspace organization can add the bot and chat with it.
- `BOT_CREATION_PLAN.md` — detailed service-by-service creation plan, including Terraform ownership, backend responsibilities, and per-environment rollout order.

---

## 0. Target Architecture

| Concept | Slack equivalent | Teams equivalent | Google Chat equivalent |
|---|---|---|---|
| App registration artifact | `infra/manifests/slack-app/*/manifest.yaml` | `infra/manifests/teams-app/*/manifest.json` (+ zip) | Values entered in the Chat API config page of a GCP project |
| Bot identity | Slack bot user + bot token | Azure Bot Service app registration (`botId`) | GCP service account + Chat API "App ID" |
| Event webhook | `/users/bots/slack/events` | `/users/bots/teams/messages` | **`/users/bots/google-chat/events`** |
| OAuth redirect | `/users/bots/slack/oauth/callback` | (delegated to Teams SSO) | **`/users/bots/google-chat/oauth/callback`** (only if user-auth scopes are needed) |
| Install flow for end users | Slack App Directory / workspace admin | Teams Admin Center / sideload zip | **Google Workspace Marketplace** listing (per-user or per-domain install) |

Our approach: build a **full Google Chat app** (not an incoming webhook). Incoming webhooks cannot receive user messages or interactions — they are one-way and space-specific. A full Chat app is the only option for an interactive AI assistant that multiple users in multiple Workspace domains need to add and chat with.

Connection mode: **HTTP endpoint** (not Pub/Sub, not Apps Script, not Dialogflow). Keeps the stack symmetric with Slack/Teams (both post events to our FastAPI backend) and avoids a GCP-only messaging dependency.

---

## 1. One-time Google Cloud Project Setup (per environment)

We maintain **one GCP project per environment**:

| Environment | GCP project (friendly name) | GCP Project ID |
|---|---|---|
| prod | **Copilot GTM** | `copilotgtm` |
| dev | **CopilotGTM Dev** | `copilotgtm-dev` |
| local | **Copilot GTM Local** | `copilotgtm-local` |

1. Open [console.cloud.google.com](https://console.cloud.google.com) and sign in with a Workspace super-admin (needed later for Marketplace publication).
2. **Create a new project** (or reuse a dedicated one). Record the numeric **Project Number** and string **Project ID**.
3. **Enable APIs** under **APIs & Services → Library**:
   - **Google Chat API** — required for the app itself.
   - **Google Workspace Marketplace SDK** — required to distribute the app to users/admins (even if visibility is "private to our domain").
   - **Admin SDK API** — required only if we plan to programmatically list Workspace users (not needed for MVP).
4. Verify enabled APIs under **APIs & Services → Enabled APIs & services**.

---

## 2. Service Account (the bot's identity to call the Chat REST API)

Used when Kai needs to **send a message into a space proactively** (e.g. async LLM result arrives after the 30-second synchronous window) or create spaces. The service account's email is what shows up as the bot author in Chat spaces, so the account itself must exist in every environment even when we avoid static keys.

### 2.1 Create the service account (all environments)

1. **IAM & Admin → Service accounts → Create service account**.
   - Name: `kai-chat-bot-{env}` (e.g. `kai-chat-bot-local`).
   - No project-level IAM roles required.
2. Note the service account email (`kai-chat-bot-{env}@{project-id}.iam.gserviceaccount.com`).

### 2.2 Credential strategy (differs per environment)

Google warns against downloading long-lived JSON keys. We follow that guidance for deployed environments and only use a key locally where federation is not available.

| Environment | Runtime | Credential method | Why |
|---|---|---|---|
| **local** | Developer laptop | **`gcloud` ADC + service account impersonation** — no key on disk | Our Workspace org enforces `iam.disableServiceAccountKeyCreation`. JSON keys cannot be created. This is Google's recommended keyless path for local dev: the human account (`tech@copilotgtm.com`) signs in once, is granted `roles/iam.serviceAccountTokenCreator` on the SA, and the Google auth library picks up short-lived impersonated tokens automatically. |
| **dev** | AKS (dev cluster) | **Workload Identity Federation** from the existing Azure Workload Identity to the GCP service account — no key | AKS pods already carry an Azure Workload Identity. Federating to GCP eliminates the static key entirely. |
| **prod** | AKS (prod cluster) | **Workload Identity Federation** — no key | Same reasoning. Meets Google's recommended authentication pattern. |

### 2.2a Local-dev setup (keyless, `gcloud` ADC impersonation)

Because `iam.disableServiceAccountKeyCreation` is enforced at org level, JSON keys are not downloadable. Instead:

1. Install `gcloud` CLI — https://cloud.google.com/sdk/docs/install.
2. Sign in with your Workspace account and select the project:

   ```bash
   gcloud auth login
   gcloud config set project copilotgtm-local
   ```

3. In the GCP console, on the `kai-chat-bot-local` service account → **Permissions → Grant access**, add **your own user** (`tech@copilotgtm.com`) with role **Service Account Token Creator** (`roles/iam.serviceAccountTokenCreator`).

4. Generate Application Default Credentials and configure impersonation:

   ```bash
   gcloud auth application-default login
   gcloud config set auth/impersonate_service_account kai-chat-bot-local@copilotgtm-local.iam.gserviceaccount.com
   ```

5. Verify:

   ```bash
   gcloud auth application-default print-access-token
   ```

The backend does not need any env var — `google.auth.default()` finds the ADC file at `~/.config/gcloud/application_default_credentials.json` and applies the impersonation target automatically.

### 2.3 Workload Identity Federation setup (dev and prod only, one-time)

Wire the AKS backend pod's Azure Workload Identity to impersonate the GCP service account so the pod gets short-lived GCP access tokens with zero stored secrets.

1. **In GCP** — `IAM & Admin → Workload Identity Federation → Create pool`:
   - Pool name: `copilot-aks-pool`.
   - Add an **Azure** provider pointing at the AKS cluster's Azure AD tenant ID.
   - Attribute mapping: `google.subject = assertion.sub`, `attribute.azure_oid = assertion.oid`.
   - Attribute condition: restrict to the specific Azure object ID of the backend pod's managed identity (so only that pod can impersonate).
2. **On the service account** `kai-chat-bot-{env}@...` — **Permissions → Grant access** to the federated principal:
   - Principal: `principal://iam.googleapis.com/projects/{project-number}/locations/global/workloadIdentityPools/copilot-aks-pool/subject/{azure-object-id}`.
   - Role: `roles/iam.serviceAccountTokenCreator`.
3. **In the backend pod** — mount a credential config file (no secrets, just identifiers) and set `GOOGLE_APPLICATION_CREDENTIALS` to its path. The Google auth library then performs `Azure token → GCP STS exchange → SA impersonation` automatically on every call.

The credential config file template (commit this, it holds no secrets):

```json
{
  "type": "external_account",
  "audience": "//iam.googleapis.com/projects/{project-number}/locations/global/workloadIdentityPools/copilot-aks-pool/providers/azure",
  "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
  "token_url": "https://sts.googleapis.com/v1/token",
  "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/kai-chat-bot-{env}@{project-id}.iam.gserviceaccount.com:generateAccessToken",
  "credential_source": {
    "url": "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=api://AzureADTokenExchange",
    "headers": { "Metadata": "True" },
    "format": { "type": "json", "subject_token_field_name": "access_token" }
  }
}
```

No JSON key is downloaded, stored, or rotated.

---

## 3. Register the Chat App in the Google Chat API Configuration Page

This is the equivalent of writing `manifest.yaml` for Slack, but done through the console UI. Values for each environment are captured in `{env}/chat-app-config.json` — copy them field-by-field into the page below.

1. With the environment's GCP project selected, go to **APIs & Services → Enabled APIs & services → Google Chat API → Configuration** (direct URL: `https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=<PROJECT_ID>`).

2. Fill in the form:

   | Section | Field | Value (see `{env}/chat-app-config.json`) |
   |---|---|---|
   | **App info** | App name | `Kai` (prod), `Kai - DEV` (dev), `Kai - LOCAL` (local) |
   | | Avatar URL | Public HTTPS URL to Kai's square PNG avatar (host it on a public CDN or `https://www.copilotgtm.com/assets/kai-avatar.png`) |
   | | Description | `AI sales assistant. @mention Kai in a space or DM Kai directly for deal insights.` |
   | **Functionality** | Receive 1:1 messages | **Checked** |
   | | Join spaces and group conversations | **Checked** |
   | **Connection settings** | App URL | `https://{api-host}/users/bots/google-chat/events` (host per env) |
   | | *(other options — Apps Script / Pub/Sub / Dialogflow — left empty)* | |
   | | Authentication audience | **App URL** (Chat signs a JWT whose `audience` claim is the App URL; our verifier validates this) |
   | **Slash commands** | `/help` | id `1`, description `Get help using Kai` |
   | | `/login` | id `2`, description `Link your CopilotGTM account` |
   | **Visibility** | Who has access | **Make Kai available to specific people and groups in `{primary-workspace-domain}`** for dev/local. **Make Kai available to everyone in `{primary-workspace-domain}`** for prod (override later with Marketplace for multi-tenant). |
   | **Logs** | Log errors to Logging | **Enabled** (visible under Cloud Logging) |

3. Click **Save**.

4. Copy the **App ID** that the console now displays — this is the immutable identifier of this environment's app. Store in `chat-app-config.json` under `appId`.

---

## 4. Implement the HTTPS Event Endpoint in the Backend

Target endpoint (already aligned with the existing `/users/bots/{platform}/...` pattern used for Slack and Teams):

```
POST https://{api-host}/users/bots/google-chat/events
```

The backend must:

1. **Verify the incoming request came from Google Chat.** Chat posts each event with an `Authorization: Bearer <JWT>` header signed by Google's service account `chat@system.gserviceaccount.com`. Verify:
   - Signature via Google's JWKS (`https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com`).
   - `iss == chat@system.gserviceaccount.com`.
   - `aud == {api-host}/users/bots/google-chat/events` (the "Authentication audience" configured in §3).
   Reject anything that fails.

2. **Dispatch on `event.type`:**

   | Event type | Meaning | Kai response |
   |---|---|---|
   | `ADDED_TO_SPACE` | Bot added to a space or DM | Return a welcome card; persist a `bot_platform_installations` row (platform = `google_chat`, external space id = `event.space.name`). |
   | `REMOVED_FROM_SPACE` | Bot removed | Mark installation as uninstalled. |
   | `MESSAGE` | User DM'd Kai or @mentioned Kai | Dispatch to the existing general agent, stream output, reply via synchronous JSON or async Chat REST API. |
   | `CARD_CLICKED` | User clicked a card button | Route to card action handler. |
   | `SLASH_COMMAND` (`event.message.slashCommand`) | User typed `/help` etc. | Route by command id. |
   | `APP_HOME` | User opened the app's homepage tab | Return the home card. |

3. **Reply synchronously (within 30 s)** as a JSON `Message` object for fast responses. For long LLM calls, acknowledge immediately, then call `https://chat.googleapis.com/v1/{space}/messages` with the service account credential once the answer is ready (symmetric to our Slack "defer and post back" pattern).

4. Existing tables (`bot_platform_installations`, `BotPlatform.GOOGLE_CHAT` already exists — confirmed in `src/domain/models/master_tables/bot_platform_installation_models.py`) are reused. No new schema needed for MVP.

---

## 5. Distribution: How End Users Add Kai

Google Chat does **not** let users "just paste an install URL" the way Slack does. The app must be listed in the **Google Workspace Marketplace** (even for internal-only distribution), then installed per-user or per-domain.

### 5a. Workspace Marketplace SDK configuration (required for every environment)

1. **APIs & Services → Google Workspace Marketplace SDK → App Configuration**:
   - **App visibility**:
     - `dev` / `local`: **Private** (visible only in our primary Workspace domain).
     - `prod`: **Public** (requires Google review — see §6).
   - **Installation settings**:
     - **Individual + admin install** — lets any Workspace user install Kai for themselves, and lets Workspace admins install it org-wide.
   - **App integrations** — check **Google Chat app** (wires Marketplace listing to the Chat app registered in §3).
   - **OAuth scopes** — list exactly what Kai calls. MVP only needs the bot scope:
     - `https://www.googleapis.com/auth/chat.bot` (no admin approval, no verification).
     - Add `https://www.googleapis.com/auth/chat.messages.readonly` only if we need to read historical messages in a space outside of events (triggers admin approval on install).
   - **Developer information** — name `CopilotGTM`, website `https://www.copilotgtm.com`, support email `support@copilotgtm.com`, privacy URL `https://www.copilotgtm.com/privacy`, terms URL `https://www.copilotgtm.com/terms`.

2. **Store Listing** tab (required for public listing, recommended for private):
   - Icon (128x128), banner (220x140 minimum), screenshots.
   - Short + detailed description, category: **Productivity**.
   - Language: English.
   - Save as draft.

3. Click **Publish** (private) or **Submit for review** (public). Private publication goes live within minutes; public review takes 2–6 weeks and requires OAuth verification if sensitive scopes are added.

### 5b. End-user install paths

Once published, users in a Workspace domain can add Kai via **any** of these paths:

1. **From the Marketplace listing**: visit `https://workspace.google.com/marketplace/app/kai/{app-id}` → **Install** → grant the OAuth consent → Kai is added to the user's Chat app list.
2. **From inside Google Chat**: in the left sidebar click **Apps → + → Find apps** → search "Kai" → **Add**.
3. **Into a specific space**: open a space → top bar menu → **Apps & integrations → Add apps** → search **Kai**.
4. **Admin org-wide install**: Workspace admin goes to [admin.google.com → Apps → Google Workspace Marketplace apps → Install app](https://admin.google.com/ac/apps/gwm) → selects Kai → installs for all users in a specified OU.

After install, `ADDED_TO_SPACE` fires, our backend persists the installation, and users can DM Kai or @mention it in any space where it was added.

---

## 6. Publication Matrix (per environment)

| Environment | GCP Project | Visibility | Install audience | Marketplace state |
|---|---|---|---|---|
| **local** | `copilotgtm-local` | **Private** (primary Workspace domain) | Only engineers whose email is on the "Specific people and groups" list in §3 | Draft — published privately so developers can install; endpoint points to `https://{dev-name}.ngrok-free.dev/users/bots/google-chat/events` |
| **dev** | `copilotgtm-dev` | **Private** (primary Workspace domain) | Everyone in `copilotgtm.com` | Published privately |
| **prod** | `copilotgtm` | **Public** (Workspace Marketplace) | Any Workspace user in any Workspace domain | Submitted for review, listing live after approval |

> Per Google: "Once published, you cannot change between private and public status." Plan the prod visibility carefully before first publication.

---

## 7. Secrets Inventory

For each environment, these live in `copilot-master-kv-{env}`:

| Secret name | Source | Purpose |
|---|---|---|
| `google-chat-service-account-json` | §2 step 2 | Bot's credential to call Chat REST API (send proactive/async messages, create spaces) |
| `google-chat-app-id` | §3 step 4 | The app's numeric ID surfaced in the console — written to `chat-app-config.json` and exported to the backend via env var `GOOGLE_CHAT_APP_ID_{ENV}` |
| `google-chat-project-number` | §1 step 2 | Used when constructing Pub/Sub resource names if we ever switch off HTTP mode |

No **signing secret** exists for Google Chat (unlike Slack's signing secret) — request authenticity is established entirely via the JWT issued by `chat@system.gserviceaccount.com` (§4 step 1).

---

## 8. End-to-End Checklist

Copy this into a tracking ticket when onboarding a new environment:

- [ ] GCP project created, Project ID recorded
- [ ] Chat API and Workspace Marketplace SDK enabled
- [ ] Service account created and keyless auth configured (local ADC impersonation or dev/prod Workload Identity Federation)
- [ ] Chat API → Configuration page filled using `{env}/chat-app-config.json`; App ID recorded
- [ ] Backend endpoint `/users/bots/google-chat/events` deployed with JWT verification (issuer + audience)
- [ ] `bot_platform_installations` table writes on `ADDED_TO_SPACE` confirmed in dev
- [ ] `/help` and `/login` slash commands round-trip
- [ ] Marketplace SDK app configuration completed (visibility, installation settings, scopes, developer info, store listing)
- [ ] Published (private) or submitted (public)
- [ ] At least one non-developer user installs Kai from the Marketplace and completes an end-to-end DM conversation

---

## References

- Google Chat Apps overview — https://developers.google.com/workspace/chat
- Authenticate & authorize Chat apps — https://developers.google.com/workspace/chat/authenticate-authorize-chat-app
- Receive & respond to interactions — https://developers.google.com/workspace/chat/receive-respond-interactions
- Workspace Marketplace publishing — https://developers.google.com/workspace/marketplace/how-to-publish
- Chat API reference — https://developers.google.com/workspace/chat/api/reference/rest
