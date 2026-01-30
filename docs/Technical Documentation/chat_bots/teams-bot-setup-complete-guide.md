# Microsoft Teams Bot Setup - Complete Step-by-Step Guide

## Table of Contents

1. [Understanding the Big Picture](#1-understanding-the-big-picture)
2. [Prerequisites - What You Need Before Starting](#2-prerequisites---what-you-need-before-starting)
3. [Phase 1: Microsoft Entra ID App Registration](#3-phase-1-microsoft-entra-id-app-registration)
4. [Phase 2: Create Azure Bot Resource](#4-phase-2-create-azure-bot-resource)
5. [Phase 3: Configure the Bot](#5-phase-3-configure-the-bot)
6. [Phase 4: Enable Microsoft Teams Channel](#6-phase-4-enable-microsoft-teams-channel)
7. [Phase 5: Create Teams App Manifest](#7-phase-5-create-teams-app-manifest)
8. [Phase 6: Local Development Setup with ngrok](#8-phase-6-local-development-setup-with-ngrok)
9. [Phase 7: Sideload and Test Your Bot](#9-phase-7-sideload-and-test-your-bot)
10. [Phase 8: Publish to Microsoft AppSource](#10-phase-8-publish-to-microsoft-appsource)
11. [Troubleshooting Common Issues](#11-troubleshooting-common-issues)
12. [Quick Reference Card](#12-quick-reference-card)

---

## 1. Understanding the Big Picture

### 1.1 What Are We Building?

Before diving into the steps, let's understand what components we're creating and why each is necessary.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        TEAMS BOT ARCHITECTURE                                │
└─────────────────────────────────────────────────────────────────────────────┘

    ┌──────────────┐
    │  Teams User  │  ← This is your customer using Microsoft Teams
    └──────┬───────┘
           │
           │ 1. User sends message
           ▼
    ┌──────────────┐
    │  Microsoft   │  ← Microsoft's cloud infrastructure
    │  Teams       │    (you don't control this)
    └──────┬───────┘
           │
           │ 2. Teams forwards message to Azure Bot Service
           ▼
    ┌──────────────┐
    │  Azure Bot   │  ← This is the "router" in Azure
    │  Service     │    It knows where to send messages
    └──────┬───────┘
           │
           │ 3. Azure Bot calls YOUR webhook URL
           ▼
    ┌──────────────┐
    │  Your        │  ← This is YOUR FastAPI backend
    │  Backend     │    Running at: https://api.yourcompany.com
    │  (Webhook)   │    Endpoint: /api/teams/messages
    └──────────────┘
```

### 1.2 Components We Will Create

| Component | What Is It | Why Do We Need It |
|-----------|------------|-------------------|
| **Microsoft Entra ID App** | An identity for your bot | Azure needs to authenticate requests to/from your bot. This is like a "username/password" for your bot. |
| **Azure Bot Resource** | A management hub in Azure | This tells Azure Bot Service where your webhook is and which channels (Teams, Slack, etc.) can use your bot. |
| **Teams Channel** | Connection between Azure Bot and Teams | Without this, Teams won't know your bot exists. |
| **App Manifest** | A JSON file describing your app | Teams reads this file to know your app's name, icon, capabilities, and bot ID. |
| **App Package** | A ZIP file with manifest + icons | This is what gets uploaded to Teams for installation. |

### 1.3 Important 2025 Updates

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ⚠️  CRITICAL DEPRECATION NOTICES                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  1. MULTI-TENANT BOTS DEPRECATED (July 31, 2025)                            ║
║     - New multi-tenant bot creation is no longer supported                   ║
║     - Use SINGLE-TENANT or User-Assigned Managed Identity                    ║
║     - Existing multi-tenant bots continue to work                           ║
║                                                                              ║
║  2. BOT FRAMEWORK SDK ARCHIVED (December 31, 2025)                          ║
║     - No longer maintained or supported                                      ║
║     - Consider Microsoft 365 Agents SDK for new projects                    ║
║     - Existing bots using Bot Framework SDK still work                      ║
║                                                                              ║
║  RECOMMENDATION: Use Single-Tenant bot + AppSource distribution             ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 2. Prerequisites - What You Need Before Starting

### 2.1 Accounts Required

| Account | URL | Cost | Why Needed |
|---------|-----|------|------------|
| Microsoft Azure | https://portal.azure.com | Pay-as-you-go (Bot Service has free tier) | To create Azure Bot resource |
| Microsoft 365 Developer | https://developer.microsoft.com/microsoft-365/dev-program | Free | To test your bot in Teams |
| Microsoft Partner Center | https://partner.microsoft.com | Free | Only if publishing to AppSource |

### 2.2 Check: Do You Have an Azure Account?

**Step 1:** Open your browser and go to:
```
https://portal.azure.com
```

**Step 2:** If you see a login page:
- You have an account → Login and continue
- You don't have an account → Click "Create one" and follow signup

**Step 3:** After login, you should see the Azure Portal Dashboard:
```
┌─────────────────────────────────────────────────────────────────┐
│  Microsoft Azure                                    [?] [⚙️] [👤]│
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Welcome to Azure                                               │
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │ + Create a  │  │ Azure       │  │ Resource    │             │
│  │   resource  │  │ services    │  │ groups      │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.3 Check: Do You Have Microsoft Teams Access?

**Step 1:** Open Microsoft Teams (desktop app or web):
```
https://teams.microsoft.com
```

**Step 2:** Verify you can access Teams with a Microsoft 365 account

**Step 3:** Check if custom apps are enabled:
- Click on "Apps" in the left sidebar
- Look for "Upload a custom app" option at the bottom
- If you don't see it, contact your IT admin to enable sideloading

### 2.4 Tools to Install

| Tool | Download URL | Purpose |
|------|-------------|---------|
| **ngrok** | https://ngrok.com/download | Creates public URL for local development |
| **Code Editor** | VS Code recommended | Edit manifest files |
| **ZIP tool** | Built into OS | Create app package |

#### Installing ngrok

**Step 1:** Go to https://ngrok.com/download

**Step 2:** Download for your operating system (Windows/Mac/Linux)

**Step 3:** Extract the downloaded file

**Step 4:** Sign up for free ngrok account at https://ngrok.com/signup

**Step 5:** After signup, go to https://dashboard.ngrok.com/get-started/your-authtoken

**Step 6:** Copy your authtoken and run:
```bash
ngrok config add-authtoken YOUR_AUTH_TOKEN_HERE
```

### 2.5 Information to Gather

Before starting, have this information ready:

| Information | Example | Where to Get |
|-------------|---------|--------------|
| Your backend webhook URL | `https://api.yourcompany.com/api/teams/messages` | Your deployment |
| App name | "CopilotGTM" | Your choice |
| App description | "AI-powered sales assistant" | Your choice |
| Privacy policy URL | `https://yourcompany.com/privacy` | Your website |
| Terms of use URL | `https://yourcompany.com/terms` | Your website |
| Support email | support@yourcompany.com | Your email |
| App icons | 192x192 color, 32x32 outline | Design team |

---

## 3. Phase 1: Microsoft Entra ID App Registration

### 3.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: Microsoft Entra ID App Registration                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Think of this as creating an "identity card" for your bot.                 ║
║                                                                              ║
║  When Teams sends a message to your bot:                                    ║
║  1. Azure Bot Service needs to PROVE the message is really from Teams       ║
║  2. Your bot needs to PROVE it's really your bot (not an imposter)          ║
║                                                                              ║
║  The App Registration provides:                                              ║
║  • Application (Client) ID → Your bot's "username"                          ║
║  • Client Secret → Your bot's "password"                                    ║
║  • Tenant ID → Which Azure organization owns this bot                       ║
║                                                                              ║
║  WITHOUT THIS: Azure Bot Service cannot authenticate your bot, and          ║
║  messages will fail with "Unauthorized" errors.                             ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 3.2 Step-by-Step: Create App Registration

#### Step 1: Open Microsoft Entra Admin Center

**Action:** Open your browser and go to:
```
https://entra.microsoft.com
```

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Microsoft Entra admin center                       [?] [⚙️] [👤]│
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐                                               │
│  │ ≡ Menu       │   Welcome to Microsoft Entra admin center     │
│  │              │                                               │
│  │ Overview     │   Manage all of your identity and access      │
│  │ Identity     │   solutions from here.                        │
│  │ Protection   │                                               │
│  │ ...          │                                               │
│  └──────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 2: Navigate to App Registrations

**Action:** In the left menu:
1. Click **"Identity"** to expand it
2. Click **"Applications"** to expand it
3. Click **"App registrations"**

**Alternative direct URL:**
```
https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade
```

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  App registrations                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  [+ New registration]  [Endpoints]  [Troubleshooting]           │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Owned applications │ All applications │ Deleted apps    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  No applications found                                          │
│  (or list of existing apps)                                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 3: Click "New registration"

**Action:** Click the **"+ New registration"** button at the top

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Register an application                                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Name *                                                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                                                         │   │
│  └─────────────────────────────────────────────────────────┘   │
│  The user-facing display name for this application              │
│                                                                 │
│  Supported account types                                        │
│  ○ Accounts in this organizational directory only               │
│    (Single tenant)                                              │
│  ○ Accounts in any organizational directory                     │
│    (Any Microsoft Entra ID tenant - Multitenant)                │
│  ○ Accounts in any organizational directory and personal        │
│    Microsoft accounts                                           │
│  ○ Personal Microsoft accounts only                             │
│                                                                 │
│  Redirect URI (optional)                                        │
│  [Select a platform ▼] [                                    ]   │
│                                                                 │
│              [Register]                                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 4: Fill in Registration Details

**Action:** Fill in the form:

| Field | Value | Explanation |
|-------|-------|-------------|
| **Name** | `CopilotGTM-Teams-Bot` | This is a display name. Users will see this when consenting to your app. Choose something descriptive. |
| **Supported account types** | Select: **"Accounts in any organizational directory (Any Microsoft Entra ID tenant - Multitenant)"** | **IMPORTANT:** Even though we're creating a single-tenant Azure Bot, the App Registration should be multi-tenant to allow users from other organizations to authenticate with your bot. |
| **Redirect URI** | Leave empty for now | We'll add this later if needed |

```
┌─────────────────────────────────────────────────────────────────┐
│  Register an application                                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Name *                                                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ CopilotGTM-Teams-Bot                                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Supported account types                                        │
│  ○ Accounts in this organizational directory only               │
│  ● Accounts in any organizational directory  ← SELECT THIS      │
│  ○ Accounts in any organizational directory and personal        │
│  ○ Personal Microsoft accounts only                             │
│                                                                 │
│              [Register]                                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 5: Click "Register"

**Action:** Click the **"Register"** button

**What happens:**
- Microsoft creates your App Registration
- You're redirected to the app's Overview page
- Two important IDs are generated

#### Step 6: Copy Important IDs

**What you'll see (Overview page):**
```
┌─────────────────────────────────────────────────────────────────┐
│  CopilotGTM-Teams-Bot | Overview                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Essentials                                                     │
│  ─────────────────────────────────────────────────────────────  │
│                                                                 │
│  Display name          CopilotGTM-Teams-Bot                     │
│                                                                 │
│  Application (client) ID                                        │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ a1b2c3d4-e5f6-7890-abcd-ef1234567890        [📋 Copy]   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Directory (tenant) ID                                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ 12345678-90ab-cdef-1234-567890abcdef        [📋 Copy]   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Object ID                                                      │
│  98765432-10fe-dcba-0987-654321fedcba                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Copy and save these values:

| Value | Where to Save | Example |
|-------|---------------|---------|
| **Application (client) ID** | Save as `BOT_APP_ID` in your notes | `a1b2c3d4-e5f6-7890-abcd-ef1234567890` |
| **Directory (tenant) ID** | Save as `BOT_TENANT_ID` in your notes | `12345678-90ab-cdef-1234-567890abcdef` |

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  📝 SAVE THESE VALUES - YOU WILL NEED THEM LATER                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  BOT_APP_ID = a1b2c3d4-e5f6-7890-abcd-ef1234567890                          ║
║  BOT_TENANT_ID = 12345678-90ab-cdef-1234-567890abcdef                       ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

#### Step 7: Create a Client Secret

**Why:** The Client Secret is like a password for your bot. Your backend uses it to prove its identity.

**Action:** In the left menu, click **"Certificates & secrets"**

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  CopilotGTM-Teams-Bot | Certificates & secrets                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌────────────────────────────────────────────────────────────┐│
│  │ Certificates │ Client secrets │ Federated credentials      ││
│  └────────────────────────────────────────────────────────────┘│
│                                                                 │
│  Client secrets                                                 │
│  A secret string that the application uses to prove its         │
│  identity when requesting a token.                              │
│                                                                 │
│  [+ New client secret]                                          │
│                                                                 │
│  No client secrets have been created for this application.      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Click **"+ New client secret"**

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Add a client secret                                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Description                                                    │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Teams Bot Secret                                        │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Expires                                                        │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ 180 days (6 months)                              ▼      │   │
│  │ ─────────────────────────────────────────────────────── │   │
│  │ 90 days (3 months)                                      │   │
│  │ 180 days (6 months)                                     │   │
│  │ 365 days (12 months)                                    │   │
│  │ 545 days (18 months)                                    │   │
│  │ 730 days (24 months)  ← RECOMMENDED                     │   │
│  │ Custom                                                  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│              [Add]                                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Fill in:

| Field | Value |
|-------|-------|
| Description | `Teams Bot Secret` |
| Expires | `730 days (24 months)` - for production, you'll need to rotate this before expiry |

**Action:** Click **"Add"**

#### Step 8: IMMEDIATELY Copy the Secret Value

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ⚠️  CRITICAL: COPY THE SECRET NOW!                                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  The secret VALUE is only shown ONCE.                                        ║
║  After you leave this page, you can NEVER see it again.                     ║
║  If you lose it, you must create a new secret.                              ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Client secrets                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Description        Secret ID            Value          Expires │
│  ─────────────────────────────────────────────────────────────  │
│  Teams Bot Secret   abc123...            Xyz789...~Q    1/28/28 │
│                                          [📋 Copy]              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Click **"Copy"** next to the Value column

**Save this value:**
```
╔══════════════════════════════════════════════════════════════════════════════╗
║  📝 SAVE THIS VALUE - YOU WILL NEED IT LATER                                ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  BOT_APP_SECRET = Xyz789...~Q_YOUR_ACTUAL_SECRET_HERE                       ║
║                                                                              ║
║  (This is an example - your actual secret will be different)                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

#### Step 9: Add Redirect URI for Bot Framework

**Why:** Bot Framework uses this URL to authenticate your bot.

**Action:** In the left menu, click **"Authentication"**

**Action:** Click **"+ Add a platform"**

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Configure platforms                                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │   Web    │  │  SPA     │  │  iOS/    │  │ Android  │        │
│  │          │  │          │  │  macOS   │  │          │        │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘        │
│                                                                 │
│  ┌──────────┐  ┌──────────┐                                    │
│  │ Mobile & │  │ Windows  │                                    │
│  │ desktop  │  │          │                                    │
│  └──────────┘  └──────────┘                                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Click **"Web"**

**Action:** Enter this Redirect URI:
```
https://token.botframework.com/.auth/web/redirect
```

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Configure Web                                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Redirect URIs                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ https://token.botframework.com/.auth/web/redirect       │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│              [Configure]                                        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Click **"Configure"**

### 3.3 Summary: What We Created

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 1 COMPLETE: Microsoft Entra ID App Registration                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You have created:                                                           ║
║  • An App Registration named "CopilotGTM-Teams-Bot"                         ║
║  • A Client Secret for authentication                                        ║
║  • A Redirect URI for Bot Framework                                         ║
║                                                                              ║
║  Values to save:                                                             ║
║  • BOT_APP_ID = your Application (client) ID                                ║
║  • BOT_TENANT_ID = your Directory (tenant) ID                               ║
║  • BOT_APP_SECRET = your Client Secret value                                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 4. Phase 2: Create Azure Bot Resource

### 4.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: Azure Bot Resource                                                     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  The Azure Bot Resource is the "control center" for your bot.               ║
║                                                                              ║
║  It provides:                                                                ║
║  • A place to set your webhook URL (messaging endpoint)                     ║
║  • Channel management (Teams, Slack, Web Chat, etc.)                        ║
║  • Analytics and monitoring                                                  ║
║  • Configuration management                                                  ║
║                                                                              ║
║  Think of it as a "router" - Teams sends messages to Azure Bot Service,     ║
║  and Azure Bot Service forwards them to YOUR webhook URL.                   ║
║                                                                              ║
║  WITHOUT THIS: There's no way for Teams to reach your backend.              ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 4.2 Step-by-Step: Create Azure Bot

#### Step 1: Open Azure Portal

**Action:** Open your browser and go to:
```
https://portal.azure.com
```

**Login** with your Azure account.

#### Step 2: Create a Resource

**Action:** Click **"+ Create a resource"** (top-left, or center of dashboard)

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Create a resource                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Search services and marketplace                                │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ 🔍 Search...                                            │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Popular Azure services                                         │
│  • Virtual machine                                              │
│  • Storage account                                              │
│  • Web App                                                      │
│  • SQL Database                                                 │
│  ...                                                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 3: Search for "Azure Bot"

**Action:** In the search box, type:
```
Azure Bot
```

**Action:** Press Enter or click the search icon

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Marketplace                                                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Search results for "Azure Bot"                                 │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  🤖 Azure Bot                                           │   │
│  │  Microsoft                                               │   │
│  │  Create a bot that can interact with users across        │   │
│  │  multiple channels like Teams, Slack, Web Chat...        │   │
│  │                                            [Create ▼]    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 4: Click "Create"

**Action:** Click the **"Create"** button on the Azure Bot card

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Create an Azure Bot                                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Basics   Review + create                                       │
│  ───────────────────────────────────────────────────────────────│
│                                                                 │
│  Project details                                                │
│                                                                 │
│  Subscription *                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Pay-As-You-Go                                      ▼    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Resource group *                                               │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ (Select or create new)                             ▼    │   │
│  └─────────────────────────────────────────────────────────┘   │
│  [Create new]                                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 5: Fill in Bot Details

**Section: Project details**

| Field | Action | Value | Explanation |
|-------|--------|-------|-------------|
| Subscription | Select | Your subscription | The Azure subscription to bill |
| Resource group | Click "Create new" | `rg-copilotgtm-bot` | A container to organize related resources |

**Creating a new Resource Group:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Create a resource group                                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Name *                                                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ rg-copilotgtm-bot                                       │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│              [OK]                                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Section: Instance details**

| Field | Value | Explanation |
|-------|-------|-------------|
| **Bot handle** | `copilotgtm-teams-bot` | Globally unique identifier. Use lowercase, numbers, hyphens only. |
| **Pricing tier** | Select **"F0 (Free)"** for development, **"S1"** for production | Free tier has limits: 10,000 messages/month |

```
┌─────────────────────────────────────────────────────────────────┐
│  Instance details                                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Bot handle *                                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ copilotgtm-teams-bot                                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│  This is a globally unique identifier for your bot.             │
│                                                                 │
│  Pricing tier *                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ F0 (10K Premium Messages)                          ▼    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Section: Microsoft App ID**

This is where you connect your Entra ID App Registration to this bot.

| Field | Value | Explanation |
|-------|-------|-------------|
| **Type of App** | Select **"Single Tenant"** | IMPORTANT: Multi-tenant is deprecated |
| **Creation type** | Select **"Use existing app registration"** | We created one in Phase 1 |
| **App ID** | Paste your `BOT_APP_ID` from Phase 1 | Example: `a1b2c3d4-e5f6-7890-abcd-ef1234567890` |
| **App tenant ID** | Paste your `BOT_TENANT_ID` from Phase 1 | Your Azure tenant ID |
| **App password** | Paste your `BOT_APP_SECRET` from Phase 1 | The client secret you created |

```
┌─────────────────────────────────────────────────────────────────┐
│  Microsoft App ID                                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Type of App *                                                  │
│  ○ User-Assigned Managed Identity                               │
│  ● Single Tenant                       ← SELECT THIS            │
│  ○ Multi Tenant (Deprecated after July 2025)                    │
│                                                                 │
│  Creation type                                                  │
│  ○ Create new Microsoft App ID                                  │
│  ● Use existing app registration       ← SELECT THIS            │
│                                                                 │
│  App ID *                                                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ a1b2c3d4-e5f6-7890-abcd-ef1234567890                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  App tenant ID *                                                │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ 12345678-90ab-cdef-1234-567890abcdef                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  App password *                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ ••••••••••••••••••••••••••••••••                        │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 6: Review and Create

**Action:** Click **"Review + create"**

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Create an Azure Bot                                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ✓ Validation passed                                            │
│                                                                 │
│  Basics                                                         │
│  ─────────────────────────────────────────────────────────────  │
│  Subscription         Pay-As-You-Go                             │
│  Resource group       rg-copilotgtm-bot                         │
│  Bot handle           copilotgtm-teams-bot                      │
│  Pricing tier         F0 (Free)                                 │
│  Type of App          Single Tenant                             │
│  App ID               a1b2c3d4-e5f6-...                         │
│                                                                 │
│              [Create]                                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Click **"Create"**

#### Step 7: Wait for Deployment

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Deployment                                                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ⏳ Deployment is in progress...                                │
│                                                                 │
│  Deployment name    Microsoft.BotService-xxx                    │
│  Subscription       Pay-As-You-Go                               │
│  Resource group     rg-copilotgtm-bot                           │
│                                                                 │
│  ──────────────────────────────────────────────────────────     │
│  ✓ Creating Azure Bot resource...                    Complete   │
│  ⏳ Configuring channels...                          In progress│
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

Wait 1-2 minutes for deployment to complete.

#### Step 8: Go to Resource

**What you'll see after deployment completes:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Deployment                                                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ✅ Your deployment is complete                                 │
│                                                                 │
│              [Go to resource]                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Action:** Click **"Go to resource"**

### 4.3 Summary: What We Created

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 2 COMPLETE: Azure Bot Resource                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You have created:                                                           ║
║  • An Azure Bot resource named "copilotgtm-teams-bot"                       ║
║  • Connected to your Entra ID App Registration                              ║
║  • Single-tenant configuration (future-proof)                               ║
║                                                                              ║
║  The bot is NOT YET connected to Teams. We'll do that in Phase 4.           ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 5. Phase 3: Configure the Bot

### 5.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: Bot Configuration                                                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  The most critical setting is the MESSAGING ENDPOINT.                       ║
║                                                                              ║
║  This is YOUR backend URL where Azure Bot Service will send messages.       ║
║                                                                              ║
║  Example: https://api.yourcompany.com/api/teams/messages                    ║
║                                                                              ║
║  When a user sends "Hello" in Teams:                                        ║
║  1. Teams sends it to Azure Bot Service                                     ║
║  2. Azure Bot Service POSTs it to YOUR messaging endpoint                   ║
║  3. Your backend processes it and responds                                  ║
║                                                                              ║
║  WITHOUT THIS: Azure Bot Service doesn't know where to send messages.       ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 5.2 Step-by-Step: Configure Messaging Endpoint

#### Step 1: Navigate to Configuration

**Starting point:** You should be on your Azure Bot resource page after Phase 2.

**If not, navigate there:**
1. Go to https://portal.azure.com
2. Search for your bot name: `copilotgtm-teams-bot`
3. Click on the bot resource

**Action:** In the left menu, under **"Settings"**, click **"Configuration"**

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  copilotgtm-teams-bot | Configuration                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ⚠ Your bot does not have a messaging endpoint configured.     │
│    Configure a messaging endpoint below.                        │
│                                                                 │
│  ─────────────────────────────────────────────────────────────  │
│                                                                 │
│  Messaging endpoint                                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ https://                                                │   │
│  └─────────────────────────────────────────────────────────┘   │
│  The endpoint your bot listens on for messages                  │
│                                                                 │
│  Microsoft App ID                                               │
│  a1b2c3d4-e5f6-7890-abcd-ef1234567890                          │
│                                                                 │
│  ☑ Enable Streaming Endpoint                                   │
│                                                                 │
│              [Apply]                                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 2: Enter Your Messaging Endpoint

**For Production:**
```
https://api.yourcompany.com/api/teams/messages
```

**For Local Development (we'll set this up in Phase 6):**
```
https://YOUR-NGROK-URL.ngrok.io/api/teams/messages
```

**For now, if your backend is not deployed yet, you can leave this empty or use a placeholder.**

**Action:** Enter your messaging endpoint URL

```
┌─────────────────────────────────────────────────────────────────┐
│  Messaging endpoint                                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ https://api.yourcompany.com/api/teams/messages          │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 3: Ensure Streaming is Enabled

**Action:** Make sure **"Enable Streaming Endpoint"** is checked

```
☑ Enable Streaming Endpoint
```

**Why:** Streaming allows your bot to send long responses in chunks, improving user experience.

#### Step 4: Apply Changes

**Action:** Click **"Apply"** to save

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  ✓ Successfully updated bot configuration                      │
└─────────────────────────────────────────────────────────────────┘
```

### 5.3 Summary: What We Configured

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 3 COMPLETE: Bot Configuration                                     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You have configured:                                                        ║
║  • Messaging endpoint (where Azure sends messages)                          ║
║  • Streaming enabled (for better UX)                                        ║
║                                                                              ║
║  The endpoint format is:                                                     ║
║  https://YOUR-DOMAIN/api/teams/messages                                     ║
║                                                                              ║
║  This endpoint must:                                                         ║
║  • Be publicly accessible via HTTPS                                         ║
║  • Accept POST requests                                                      ║
║  • Return 200 OK for valid Bot Framework activities                         ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 6. Phase 4: Enable Microsoft Teams Channel

### 6.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: Teams Channel                                                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Azure Bot Service can connect to MANY channels:                            ║
║  • Microsoft Teams                                                           ║
║  • Slack                                                                     ║
║  • Facebook Messenger                                                        ║
║  • Web Chat                                                                  ║
║  • Email                                                                     ║
║  • And more...                                                               ║
║                                                                              ║
║  By default, NO channels are enabled (except Web Chat).                     ║
║                                                                              ║
║  We need to explicitly enable the Teams channel so that:                    ║
║  • Teams knows this bot exists                                              ║
║  • Teams can route messages to Azure Bot Service                            ║
║  • The bot appears in Teams search results                                  ║
║                                                                              ║
║  WITHOUT THIS: Users cannot find or interact with your bot in Teams.        ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 6.2 Step-by-Step: Enable Teams Channel

#### Step 1: Navigate to Channels

**Starting point:** Your Azure Bot resource page

**Action:** In the left menu, under **"Settings"**, click **"Channels"**

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  copilotgtm-teams-bot | Channels                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Connect to channels                                            │
│  Add channels to your bot to make it available on messaging     │
│  platforms.                                                     │
│                                                                 │
│  ─────────────────────────────────────────────────────────────  │
│                                                                 │
│  Available Channels                                             │
│                                                                 │
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐   │
│  │ Teams  │  │ Slack  │  │ Web    │  │ Email  │  │ Direct │   │
│  │   🟣   │  │   #    │  │ Chat   │  │   📧   │  │ Line   │   │
│  └────────┘  └────────┘  └────────┘  └────────┘  └────────┘   │
│                                                                 │
│  Connected Channels                                             │
│                                                                 │
│  Channel          Status        Actions                         │
│  ──────────────────────────────────────────────────────────    │
│  Web Chat         Running       [Edit] [Delete]                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 2: Click on Microsoft Teams

**Action:** Click on the **"Microsoft Teams"** icon (purple Teams logo)

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Configure Microsoft Teams                                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Terms of Service                                               │
│                                                                 │
│  By clicking "Apply" below, you agree to the Microsoft          │
│  Channel Publication Terms and the Microsoft Privacy Statement. │
│                                                                 │
│  ☑ I agree to the Microsoft Channel Publication Terms and       │
│    Microsoft Privacy Statement                                  │
│                                                                 │
│  ─────────────────────────────────────────────────────────────  │
│                                                                 │
│  Messaging                                                      │
│                                                                 │
│  Which Microsoft Teams environment(s) should this bot work in?  │
│                                                                 │
│  ● Microsoft Teams Commercial  ← SELECT THIS (most common)      │
│  ○ Microsoft Teams for US Government (GCC)                      │
│  ○ Microsoft Teams for US Government (GCCH)                     │
│                                                                 │
│              [Apply]    [Cancel]                                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 3: Accept Terms and Configure

**Action:**
1. Check the box to agree to Terms of Service
2. Select **"Microsoft Teams Commercial"** (unless you're in government)
3. Click **"Apply"**

```
☑ I agree to the Microsoft Channel Publication Terms...

● Microsoft Teams Commercial
```

**What happens:**
- Azure Bot Service registers your bot with Microsoft Teams
- Your bot can now receive messages from Teams
- A few seconds later, you'll see Teams in "Connected Channels"

#### Step 4: Verify Channel is Connected

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Connected Channels                                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Channel               Status        Actions                    │
│  ──────────────────────────────────────────────────────────    │
│  Microsoft Teams       Running       [Edit] [Delete]            │
│  Web Chat              Running       [Edit] [Delete]            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 6.3 Summary: What We Enabled

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 4 COMPLETE: Teams Channel                                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You have enabled:                                                           ║
║  • Microsoft Teams channel for your bot                                     ║
║  • Your bot can now receive messages from Teams                             ║
║                                                                              ║
║  However, the bot is NOT YET visible to users.                              ║
║  We need to create an App Manifest and install it in Teams.                 ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 7. Phase 5: Create Teams App Manifest

### 7.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: Teams App Manifest                                                     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Microsoft Teams doesn't just let any bot connect to it.                    ║
║  Teams needs to know:                                                        ║
║  • What is this bot called?                                                 ║
║  • What can it do?                                                           ║
║  • What does it look like (icons)?                                          ║
║  • Who made it?                                                              ║
║  • What permissions does it need?                                           ║
║                                                                              ║
║  The App Manifest is a JSON file that answers all these questions.          ║
║                                                                              ║
║  The App Package is a ZIP file containing:                                  ║
║  • manifest.json - The configuration file                                   ║
║  • color.png - 192x192 pixel full-color icon                               ║
║  • outline.png - 32x32 pixel outline icon                                   ║
║                                                                              ║
║  WITHOUT THIS: You cannot install your bot in Teams.                        ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 7.2 Step-by-Step: Create the Manifest

#### Step 1: Create a Folder for Your App

**Action:** Create a folder on your computer:
```
teams-app/
├── manifest.json
├── color.png
└── outline.png
```

#### Step 2: Create the Icons

**color.png Requirements:**
- Size: 192 x 192 pixels
- Format: PNG
- Full color with transparent background recommended
- This appears in the Teams app catalog and app details

**outline.png Requirements:**
- Size: 32 x 32 pixels
- Format: PNG
- Single color (white) with transparent background
- This appears in the Teams activity bar and app tray

```
┌─────────────────────────────────────────────────────────────────┐
│  Icon Examples                                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  color.png (192x192)           outline.png (32x32)             │
│                                                                 │
│  ┌──────────────────┐          ┌────────┐                      │
│  │                  │          │        │                      │
│  │   Your Logo      │          │  ○─○   │  (white outline     │
│  │   Full Color     │          │        │   on transparent)   │
│  │                  │          └────────┘                      │
│  │                  │                                          │
│  └──────────────────┘                                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**If you don't have icons yet:** Create simple placeholder icons using any image editor, or use online tools like:
- https://www.canva.com
- https://www.figma.com

#### Step 3: Create manifest.json

**Action:** Create a file named `manifest.json` with this content:

```json
{
  "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
  "manifestVersion": "1.17",
  "version": "1.0.0",
  "id": "YOUR_BOT_APP_ID_HERE",
  "developer": {
    "name": "Your Company Name",
    "websiteUrl": "https://yourcompany.com",
    "privacyUrl": "https://yourcompany.com/privacy",
    "termsOfUseUrl": "https://yourcompany.com/terms"
  },
  "name": {
    "short": "CopilotGTM",
    "full": "CopilotGTM AI Sales Assistant"
  },
  "description": {
    "short": "AI-powered sales assistant",
    "full": "CopilotGTM helps you find answers from your organization's documentation and deal information. Ask questions about products, pricing, competitors, and more."
  },
  "icons": {
    "color": "color.png",
    "outline": "outline.png"
  },
  "accentColor": "#5558AF",
  "bots": [
    {
      "botId": "YOUR_BOT_APP_ID_HERE",
      "scopes": [
        "personal",
        "team",
        "groupchat"
      ],
      "supportsFiles": false,
      "isNotificationOnly": false,
      "commandLists": [
        {
          "scopes": ["personal"],
          "commands": [
            {
              "title": "login",
              "description": "Sign in to your CopilotGTM account"
            },
            {
              "title": "logout",
              "description": "Sign out of your account"
            },
            {
              "title": "status",
              "description": "Check your authentication status"
            },
            {
              "title": "help",
              "description": "Get help using the bot"
            }
          ]
        }
      ]
    }
  ],
  "permissions": [
    "identity",
    "messageTeamMembers"
  ],
  "validDomains": [
    "yourcompany.com",
    "api.yourcompany.com",
    "token.botframework.com"
  ]
}
```

#### Step 4: Replace Placeholders

**IMPORTANT:** You MUST replace these placeholders with your actual values:

| Placeholder | Replace With | Example |
|-------------|--------------|---------|
| `YOUR_BOT_APP_ID_HERE` | Your Application (client) ID from Phase 1 | `a1b2c3d4-e5f6-7890-abcd-ef1234567890` |
| `Your Company Name` | Your actual company name | `Acme Inc.` |
| `yourcompany.com` | Your actual domain | `acme.com` |
| `https://yourcompany.com/privacy` | Your privacy policy URL | `https://acme.com/privacy` |
| `https://yourcompany.com/terms` | Your terms of use URL | `https://acme.com/terms` |

**There are TWO places where `YOUR_BOT_APP_ID_HERE` appears:**
1. In the `"id"` field at the top
2. In the `"botId"` field under `"bots"`

**Both MUST be the same value - your Application (client) ID.**

#### Step 5: Understanding the Manifest Fields

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  MANIFEST FIELD EXPLANATIONS                                                 │
└─────────────────────────────────────────────────────────────────────────────┘

"$schema": Points to Microsoft's schema for validation
"manifestVersion": "1.17" - The schema version we're using

"id": YOUR APPLICATION (CLIENT) ID
      This MUST match the App ID from Entra ID registration.
      Teams uses this to identify your app.

"developer": Information about your company
  - "name": Shown in app details
  - "websiteUrl": Link to your website
  - "privacyUrl": REQUIRED - Your privacy policy
  - "termsOfUseUrl": REQUIRED - Your terms of service

"name":
  - "short": Up to 30 characters, shown in app catalog
  - "full": Up to 100 characters, shown in app details

"description":
  - "short": Up to 80 characters, shown in app cards
  - "full": Up to 4000 characters, shown in app details

"icons":
  - "color": 192x192 full color icon filename
  - "outline": 32x32 outline icon filename

"accentColor": Hex color code for accent (used in cards, etc.)

"bots": Array of bot configurations
  - "botId": MUST match your Application (client) ID
  - "scopes": Where the bot can be used:
      - "personal": 1-on-1 chat with user
      - "team": In a team channel
      - "groupchat": In a group chat
  - "commandLists": Suggested commands shown to users

"permissions": What the bot can do
  - "identity": Access user identity
  - "messageTeamMembers": Send messages

"validDomains": Domains the bot can open/trust
  - Your domain(s)
  - "token.botframework.com" - REQUIRED for auth
```

#### Step 6: Create the App Package (ZIP File)

**Action:** Select all three files and create a ZIP archive:

**On Windows:**
1. Open the `teams-app` folder
2. Select all three files (manifest.json, color.png, outline.png)
3. Right-click → "Send to" → "Compressed (zipped) folder"
4. Rename to `copilotgtm-teams-app.zip`

**On Mac:**
1. Open the `teams-app` folder
2. Select all three files
3. Right-click → "Compress 3 items"
4. Rename to `copilotgtm-teams-app.zip`

**On Linux/Command Line:**
```bash
cd teams-app
zip copilotgtm-teams-app.zip manifest.json color.png outline.png
```

**IMPORTANT:** The ZIP file should contain the files at the ROOT level, not inside a folder:
```
copilotgtm-teams-app.zip
├── manifest.json     ✅ CORRECT: Files at root
├── color.png
└── outline.png

NOT like this:
copilotgtm-teams-app.zip
└── teams-app/        ❌ WRONG: Files inside folder
    ├── manifest.json
    ├── color.png
    └── outline.png
```

### 7.3 Summary: What We Created

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 5 COMPLETE: Teams App Manifest                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You have created:                                                           ║
║  • manifest.json with your bot configuration                                ║
║  • color.png (192x192) icon                                                 ║
║  • outline.png (32x32) icon                                                 ║
║  • copilotgtm-teams-app.zip package                                         ║
║                                                                              ║
║  This package can now be:                                                    ║
║  • Sideloaded into Teams for testing (Phase 7)                              ║
║  • Published to AppSource for public distribution (Phase 8)                 ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 8. Phase 6: Local Development Setup with ngrok

### 8.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: ngrok for Local Development                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Microsoft Teams runs in the cloud. Your development server runs locally.   ║
║                                                                              ║
║  Problem:                                                                    ║
║  • Teams cannot reach localhost:8000                                        ║
║  • Your firewall blocks incoming connections                                ║
║  • You need HTTPS (Teams requires it)                                       ║
║                                                                              ║
║  Solution: ngrok                                                             ║
║  • Creates a secure tunnel from the internet to your localhost              ║
║  • Provides an HTTPS URL that Teams can reach                               ║
║  • Your local server receives the requests                                  ║
║                                                                              ║
║  ┌────────────┐      ┌─────────────┐      ┌───────────────┐                ║
║  │   Teams    │ ──▶  │   ngrok     │ ──▶  │  localhost    │                ║
║  │            │      │   cloud     │      │  :8000        │                ║
║  └────────────┘      └─────────────┘      └───────────────┘                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 8.2 Step-by-Step: Set Up ngrok

#### Step 1: Start Your Backend Server

**Action:** In your project directory, start your FastAPI server:

```bash
cd D:\CopilotGTM\copilot-backend
uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

**What you'll see:**
```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [12345]
INFO:     Started server process [12346]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

#### Step 2: Start ngrok Tunnel

**Action:** Open a NEW terminal window and run:

```bash
ngrok http 8000
```

**What you'll see:**
```
ngrok

Build better satisfying your egos
API integrations faster with ngrok. Visit https://ngrok.com/

Session Status                online
Account                       Your Name (Plan: Free)
Version                       3.x.x
Region                        United States (us)
Latency                       45ms
Web Interface                 http://127.0.0.1:4040
Forwarding                    https://abc123xyz.ngrok-free.app -> localhost:8000

Connections                   ttl     opn     rt1     rt5     p50     p90
                              0       0       0.00    0.00    0.00    0.00
```

#### Step 3: Copy the ngrok URL

**Action:** Copy the HTTPS forwarding URL:
```
https://abc123xyz.ngrok-free.app
```

**Your messaging endpoint will be:**
```
https://abc123xyz.ngrok-free.app/api/teams/messages
```

#### Step 4: Update Azure Bot Configuration

**Action:** Go back to Azure Portal:
1. Navigate to your bot resource
2. Go to Configuration
3. Update the Messaging endpoint to your ngrok URL:
   ```
   https://abc123xyz.ngrok-free.app/api/teams/messages
   ```
4. Click Apply

```
┌─────────────────────────────────────────────────────────────────┐
│  Messaging endpoint                                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ https://abc123xyz.ngrok-free.app/api/teams/messages     │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 5: Update manifest.json validDomains

**Action:** Add ngrok domain to your manifest.json:

```json
"validDomains": [
  "yourcompany.com",
  "api.yourcompany.com",
  "token.botframework.com",
  "abc123xyz.ngrok-free.app"
]
```

**Then recreate the ZIP file.**

### 8.3 Important Notes About ngrok

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ⚠️  IMPORTANT: ngrok URL Changes                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  FREE ngrok accounts get a NEW random URL every time you restart ngrok.     ║
║                                                                              ║
║  This means:                                                                 ║
║  • You must update Azure Bot Configuration each time                        ║
║  • You must update manifest.json validDomains                               ║
║  • You must re-create and re-upload the app package                        ║
║                                                                              ║
║  Solutions:                                                                  ║
║  1. Keep ngrok running during development                                   ║
║  2. Pay for ngrok (get static subdomain)                                    ║
║  3. Use Azure Dev Tunnels (alternative)                                     ║
║  4. Deploy to a staging environment                                         ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 8.4 Summary: What We Set Up

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 6 COMPLETE: Local Development Setup                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You have set up:                                                            ║
║  • ngrok tunnel to expose localhost to the internet                         ║
║  • Updated Azure Bot messaging endpoint                                     ║
║  • Updated manifest.json validDomains                                       ║
║                                                                              ║
║  Your local development flow:                                                ║
║  1. Start backend server (port 8000)                                        ║
║  2. Start ngrok (ngrok http 8000)                                          ║
║  3. Update Azure Bot endpoint with ngrok URL                                ║
║  4. Test in Teams                                                            ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 9. Phase 7: Sideload and Test Your Bot

### 9.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: Sideloading                                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  "Sideloading" means installing your app directly into Teams without        ║
║  going through the official Teams Store (AppSource).                        ║
║                                                                              ║
║  This is essential for:                                                      ║
║  • Testing during development                                                ║
║  • Internal-only apps                                                        ║
║  • Apps before AppSource publication                                        ║
║                                                                              ║
║  Your IT admin may need to enable sideloading for your organization.        ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 9.2 Step-by-Step: Enable Sideloading (Admin Required)

#### Check if Sideloading is Already Enabled

**Action:** Open Microsoft Teams and check:
1. Click "Apps" in the left sidebar
2. Look for "Upload a custom app" at the bottom

If you see it, skip to Step 9.3. If not, an admin needs to enable it.

#### Admin Steps to Enable Sideloading

**Action (requires Teams Admin):**

1. **Go to Teams Admin Center:**
   ```
   https://admin.teams.microsoft.com
   ```

2. **Navigate to App Setup Policies:**
   - Click "Teams apps" in the left menu
   - Click "Setup policies"

3. **Edit Global Policy (or create new):**
   - Click on "Global (Org-wide default)"
   - Find "Upload custom apps"
   - Toggle it **ON**

4. **Save Changes:**
   - Click "Save"
   - Wait 15-30 minutes for changes to propagate

```
┌─────────────────────────────────────────────────────────────────┐
│  Teams Admin Center - Setup Policies                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Global (Org-wide default)                                      │
│                                                                 │
│  Upload custom apps                                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                                              [ON] 🔵    │   │
│  └─────────────────────────────────────────────────────────┘   │
│  Allow users to upload custom apps                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 9.3 Step-by-Step: Upload Your App

#### Step 1: Open Microsoft Teams

**Action:** Open Teams (desktop app or web version at https://teams.microsoft.com)

#### Step 2: Navigate to Apps

**Action:** Click "Apps" in the left sidebar

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Apps                                                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  🔍 Search apps...                                              │
│                                                                 │
│  Built for your org                                             │
│  [App 1] [App 2] ...                                           │
│                                                                 │
│  Featured                                                       │
│  [Popular App 1] [Popular App 2] ...                           │
│                                                                 │
│  ─────────────────────────────────────────────────────────────  │
│                                                                 │
│  Manage your apps                                               │
│  Upload an app ▼                                                │
│    └─ Upload a custom app                                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 3: Upload Custom App

**Action:**
1. Click "Upload an app" (or "Manage your apps" → "Upload an app")
2. Click "Upload a custom app"
3. Select your `copilotgtm-teams-app.zip` file

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Add CopilotGTM                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌────────┐                                                     │
│  │  Icon  │  CopilotGTM                                        │
│  └────────┘  AI-powered sales assistant                        │
│                                                                 │
│  CopilotGTM helps you find answers from your organization's     │
│  documentation and deal information.                            │
│                                                                 │
│  Developer: Your Company Name                                   │
│  Version: 1.0.0                                                 │
│                                                                 │
│  This app can:                                                  │
│  • Send and receive messages                                   │
│  • Access your identity                                         │
│                                                                 │
│              [Add]                                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 4: Add the App

**Action:** Click "Add"

**What happens:**
- The app is installed to your Teams
- A chat window opens with your bot
- You can start interacting with it

#### Step 5: Test the Bot

**Action:** Type a message to the bot:
```
hello
```

**What you should see:**

If your backend is running and configured correctly, you'll receive a response from the bot.

If something is wrong, you'll see either:
- No response (check ngrok and backend logs)
- Error message (check Azure Bot configuration)

### 9.4 Debugging Tips

**Check ngrok logs:**
```
┌─────────────────────────────────────────────────────────────────┐
│  ngrok Web Interface: http://127.0.0.1:4040                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Open this URL in your browser to see:                          │
│  • All incoming requests                                        │
│  • Request/response bodies                                      │
│  • Status codes                                                 │
│  • Timing information                                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Check your backend logs:**
Look for incoming POST requests to `/api/teams/messages`

**Common issues:**
| Symptom | Likely Cause | Solution |
|---------|--------------|----------|
| No response | Backend not receiving requests | Check ngrok URL in Azure Bot |
| 401 Unauthorized | Wrong App ID/Secret | Verify credentials in backend config |
| 500 Error | Backend code error | Check backend logs |
| "App not found" | Wrong bot ID in manifest | Ensure manifest.json ID matches App ID |

### 9.5 Summary: What We Tested

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 7 COMPLETE: Sideload and Test                                     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You have:                                                                   ║
║  • Enabled sideloading (if needed)                                          ║
║  • Uploaded your app package to Teams                                       ║
║  • Tested the bot interaction                                               ║
║                                                                              ║
║  The bot is now working in YOUR Teams tenant.                               ║
║                                                                              ║
║  To make it available to OTHER organizations, proceed to Phase 8.           ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 10. Phase 8: Publish to Microsoft AppSource

### 10.1 Why Do We Need This?

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  WHY: AppSource Publication                                                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Currently, your bot only works in YOUR organization's Teams.               ║
║                                                                              ║
║  To allow users from OTHER organizations to use your bot, you have          ║
║  two options:                                                                ║
║                                                                              ║
║  1. APPSOURCE (Recommended for SaaS)                                        ║
║     • Submit to Microsoft AppSource (Teams Store)                           ║
║     • Microsoft reviews your app                                            ║
║     • Once approved, anyone can install it from Teams Store                 ║
║     • Takes 2-4 weeks for review                                            ║
║                                                                              ║
║  2. DIRECT SHARING                                                          ║
║     • Send .zip file to customers                                           ║
║     • Customer's IT admin uploads to their Teams                            ║
║     • No Microsoft review needed                                            ║
║     • Must be done individually per customer                                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 10.2 Prerequisites for AppSource

Before submitting, ensure you have:

| Requirement | Description |
|-------------|-------------|
| **Partner Center Account** | Developer account at https://partner.microsoft.com |
| **Privacy Policy URL** | Must be publicly accessible |
| **Terms of Use URL** | Must be publicly accessible |
| **Support Email/URL** | Users need a way to contact you |
| **App Icons** | High-quality, correct sizes |
| **Screenshots** | 1366x768 pixels, showing app in use |
| **Tested App** | Thoroughly tested in Teams |
| **Production Endpoint** | Real HTTPS URL (not ngrok) |

### 10.3 Step-by-Step: Submit to AppSource

#### Step 1: Create Partner Center Account

**Action:** Go to:
```
https://partner.microsoft.com
```

**Action:** Click "Become a partner" or "Sign in"

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Microsoft Partner Center                                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Welcome to Partner Center                                      │
│                                                                 │
│  [Become a partner]    [Sign in]                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**If new:** Follow the enrollment process:
1. Sign in with your Microsoft account
2. Provide company information
3. Accept agreements
4. Wait for approval (can take a few days)

#### Step 2: Navigate to Marketplace Offers

**Action:** After signing in:
1. Click "Marketplace offers" in the left menu
2. Or go directly to: https://partner.microsoft.com/dashboard/marketplace-offers/overview

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  Marketplace offers                                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  [+ New offer ▼]                                               │
│                                                                 │
│  Your offers                                                    │
│  ──────────────────────────────────────────────────────────    │
│  (No offers yet)                                                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 3: Create New Offer

**Action:** Click "+ New offer" → Select "Microsoft 365 and Copilot" or "Teams app"

**What you'll see:**
```
┌─────────────────────────────────────────────────────────────────┐
│  New offer                                                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Select an offer type:                                          │
│                                                                 │
│  ○ Azure Application                                            │
│  ○ Azure Container                                              │
│  ○ Azure Virtual Machine                                        │
│  ○ Consulting Service                                           │
│  ○ Dynamics 365                                                 │
│  ● Microsoft 365 and Copilot      ← SELECT THIS                │
│  ○ Power BI visual                                              │
│  ○ SaaS                                                         │
│                                                                 │
│              [Continue]                                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 4: Fill in Offer Details

**Offer setup page:**

| Field | Value |
|-------|-------|
| Offer alias | `CopilotGTM Teams Bot` (internal name) |
| Offer ID | `copilotgtm-teams-bot` (URL-safe) |

**Action:** Click "Create"

#### Step 5: Complete All Sections

The Partner Center will show you several sections to complete:

```
┌─────────────────────────────────────────────────────────────────┐
│  CopilotGTM Teams Bot - Offer overview                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ☐ Offer setup                                                  │
│  ☐ Properties                                                   │
│  ☐ Offer listing                                                │
│  ☐ Availability                                                 │
│  ☐ Technical configuration                                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Section: Offer setup**
- Select capabilities (Bot)
- Link to existing Azure Bot

**Section: Properties**
- Categories (e.g., "Productivity", "Sales")
- Industries
- Legal information

**Section: Offer listing**
- Name, description
- Screenshots (1366x768)
- Support links
- Privacy policy

**Section: Availability**
- Markets (countries)
- Pricing (free or paid)

**Section: Technical configuration**
- Upload your app package (.zip)

#### Step 6: Submit for Review

**Action:** After completing all sections:
1. Click "Review and publish"
2. Verify all information
3. Click "Publish"

**What happens:**
- Microsoft receives your submission
- Automated tests run
- Manual review by Microsoft team
- Takes 2-4 weeks typically
- You'll receive email updates

### 10.4 What Microsoft Reviews

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  MICROSOFT REVIEW PROCESS                                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Microsoft checks:                                                           ║
║                                                                              ║
║  ✓ Functionality: Does the bot work as described?                           ║
║  ✓ User Experience: Is it easy to use?                                      ║
║  ✓ Security: No malicious behavior                                          ║
║  ✓ Privacy: Proper data handling                                            ║
║  ✓ Content: Appropriate language and imagery                                ║
║  ✓ Metadata: Accurate descriptions, valid URLs                              ║
║  ✓ Technical: Manifest is valid, icons correct                              ║
║                                                                              ║
║  Common rejection reasons:                                                   ║
║  • Privacy policy missing or incomplete                                     ║
║  • Bot doesn't respond or errors out                                        ║
║  • Screenshots don't match actual app                                       ║
║  • Description is misleading                                                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 10.5 After Approval

Once approved:
- Your app appears in Microsoft Teams Store
- Any organization can find and install it
- Users from any tenant can use your bot
- You can track usage in Partner Center analytics

### 10.6 Summary: Publication Process

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ✅ PHASE 8 COMPLETE: AppSource Publication                                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Timeline:                                                                   ║
║  Day 1: Submit app to Partner Center                                        ║
║  Days 2-5: Automated testing                                                ║
║  Days 6-14: Manual review                                                   ║
║  Days 15-28: Fixes and re-review (if needed)                               ║
║  After approval: App live in Teams Store                                    ║
║                                                                              ║
║  Your bot will be available to:                                              ║
║  • Any Microsoft 365 organization                                           ║
║  • Users worldwide (based on availability settings)                         ║
║  • Searchable in Teams app catalog                                          ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 11. Troubleshooting Common Issues

### 11.1 Bot Not Responding

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  PROBLEM: Bot doesn't respond to messages                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Check 1: Is your backend running?                                          │
│  └─ Run: curl https://your-endpoint/api/teams/messages                      │
│     Expected: Should receive 405 (Method Not Allowed) - means it's up       │
│                                                                              │
│  Check 2: Is the messaging endpoint correct in Azure Bot?                   │
│  └─ Go to Azure Portal → Your Bot → Configuration                           │
│  └─ Verify the URL matches your actual endpoint                             │
│                                                                              │
│  Check 3: Is ngrok running (if local)?                                      │
│  └─ Check ngrok terminal - should show "online"                             │
│  └─ Verify ngrok URL hasn't changed                                         │
│                                                                              │
│  Check 4: Check your backend logs                                           │
│  └─ Look for incoming POST requests                                         │
│  └─ Look for any error messages                                             │
│                                                                              │
│  Check 5: Check ngrok web interface                                         │
│  └─ Open http://127.0.0.1:4040                                              │
│  └─ See if requests are reaching ngrok                                      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.2 Authentication Errors

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  PROBLEM: 401 Unauthorized or authentication failures                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Check 1: App ID matches everywhere                                         │
│  └─ Entra ID App Registration → Application (client) ID                     │
│  └─ Azure Bot → Configuration → Microsoft App ID                            │
│  └─ manifest.json → "id" field                                              │
│  └─ manifest.json → bots[0].botId                                           │
│  └─ Your backend environment variable BOT_APP_ID                            │
│  ALL MUST BE THE SAME VALUE                                                 │
│                                                                              │
│  Check 2: App Secret is correct                                             │
│  └─ Did you copy the VALUE (not the ID) of the secret?                     │
│  └─ Has the secret expired?                                                 │
│  └─ Is the secret correctly set in your backend?                           │
│                                                                              │
│  Check 3: Tenant ID (for single-tenant)                                     │
│  └─ Azure Bot must have correct tenant ID                                   │
│  └─ Your backend must use same tenant ID                                    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.3 App Won't Install

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  PROBLEM: Can't upload app or installation fails                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Check 1: ZIP file structure                                                │
│  └─ Files must be at ROOT of ZIP, not in a folder                          │
│  └─ Correct:                                                                │
│     myapp.zip                                                               │
│     ├── manifest.json                                                       │
│     ├── color.png                                                           │
│     └── outline.png                                                         │
│                                                                              │
│  Check 2: manifest.json validity                                            │
│  └─ Validate at: https://dev.teams.microsoft.com/appvalidation             │
│  └─ Check for JSON syntax errors                                            │
│  └─ Ensure all required fields are present                                  │
│                                                                              │
│  Check 3: Icon sizes                                                        │
│  └─ color.png must be exactly 192x192                                      │
│  └─ outline.png must be exactly 32x32                                      │
│                                                                              │
│  Check 4: Sideloading enabled                                               │
│  └─ Check with IT admin                                                     │
│  └─ May take 15-30 minutes after enabling                                  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.4 Bot Works Locally But Not in Production

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  PROBLEM: Works with ngrok but not production URL                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Check 1: HTTPS certificate                                                 │
│  └─ Production URL must use valid SSL certificate                           │
│  └─ Self-signed certificates won't work                                     │
│                                                                              │
│  Check 2: Firewall/Network                                                  │
│  └─ Your server must accept incoming connections from Azure                 │
│  └─ Port 443 must be open                                                   │
│                                                                              │
│  Check 3: DNS resolution                                                    │
│  └─ Your domain must resolve correctly                                      │
│  └─ Check: nslookup api.yourcompany.com                                    │
│                                                                              │
│  Check 4: Azure Bot endpoint updated                                        │
│  └─ Did you update from ngrok URL to production URL?                       │
│                                                                              │
│  Check 5: validDomains in manifest                                          │
│  └─ Your production domain must be listed                                   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 12. Quick Reference Card

### URLs You'll Need

| Purpose | URL |
|---------|-----|
| Azure Portal | https://portal.azure.com |
| Microsoft Entra admin center | https://entra.microsoft.com |
| Teams Admin Center | https://admin.teams.microsoft.com |
| Teams Developer Portal | https://dev.teams.microsoft.com |
| Partner Center | https://partner.microsoft.com |
| ngrok Dashboard | https://dashboard.ngrok.com |
| ngrok Local Interface | http://127.0.0.1:4040 |

### Values to Save

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  VALUES YOU NEED TO SAVE AND USE                                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  From Phase 1 (Entra ID App Registration):                                  ║
║  • BOT_APP_ID = Application (client) ID                                     ║
║  • BOT_TENANT_ID = Directory (tenant) ID                                    ║
║  • BOT_APP_SECRET = Client Secret VALUE                                     ║
║                                                                              ║
║  From Phase 2 (Azure Bot):                                                   ║
║  • Bot handle = your-bot-name                                               ║
║                                                                              ║
║  From Phase 6 (ngrok):                                                       ║
║  • ngrok URL = https://xxx.ngrok-free.app                                   ║
║                                                                              ║
║  Your Production:                                                            ║
║  • Backend URL = https://api.yourcompany.com                                ║
║  • Messaging endpoint = https://api.yourcompany.com/api/teams/messages      ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### Complete Phase Checklist

```
Phase 1: Microsoft Entra ID App Registration
  ☐ Created app registration
  ☐ Copied Application (client) ID
  ☐ Copied Directory (tenant) ID
  ☐ Created client secret
  ☐ Copied client secret VALUE
  ☐ Added redirect URI for Bot Framework

Phase 2: Azure Bot Resource
  ☐ Created Azure Bot resource
  ☐ Selected Single Tenant
  ☐ Used existing app registration
  ☐ Entered App ID, Tenant ID, Secret

Phase 3: Bot Configuration
  ☐ Set messaging endpoint URL
  ☐ Enabled streaming endpoint
  ☐ Applied changes

Phase 4: Teams Channel
  ☐ Opened Channels settings
  ☐ Added Microsoft Teams channel
  ☐ Accepted terms of service
  ☐ Verified channel is "Running"

Phase 5: App Manifest
  ☐ Created manifest.json
  ☐ Replaced all placeholders with real values
  ☐ Created color.png (192x192)
  ☐ Created outline.png (32x32)
  ☐ Created ZIP package

Phase 6: Local Development (ngrok)
  ☐ Started backend server
  ☐ Started ngrok tunnel
  ☐ Copied ngrok URL
  ☐ Updated Azure Bot messaging endpoint
  ☐ Updated manifest validDomains

Phase 7: Testing
  ☐ Verified sideloading is enabled
  ☐ Uploaded app package to Teams
  ☐ Sent test message to bot
  ☐ Received response from bot

Phase 8: AppSource (Optional)
  ☐ Created Partner Center account
  ☐ Created new offer
  ☐ Completed all sections
  ☐ Submitted for review
  ☐ Addressed any feedback
  ☐ App published
```

---

## Summary

Congratulations! You now have a complete guide to:

1. **Understanding** why each component is needed
2. **Creating** a Microsoft Entra ID app registration
3. **Setting up** an Azure Bot resource
4. **Configuring** the messaging endpoint
5. **Enabling** the Teams channel
6. **Building** an app manifest and package
7. **Testing** locally with ngrok
8. **Sideloading** into Teams
9. **Publishing** to AppSource

This guide covered every click, every field, and every step with explanations of WHY each step is necessary.

---

## Sources

- [Microsoft Learn - Create Azure Bot resource](https://learn.microsoft.com/en-us/azure/bot-service/abs-quickstart)
- [Microsoft Learn - How to register an app in Microsoft Entra ID](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
- [Microsoft Learn - Bots in Teams](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-a-bot)
- [Microsoft Learn - Upload custom app](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/apps-upload)
- [Microsoft Learn - Publish to AppSource](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/appsource/publish)
- [Microsoft Learn - App Manifest Schema](https://learn.microsoft.com/en-us/microsoft-365/extensibility/schema/)
- [ngrok Documentation - Teams Webhooks](https://ngrok.com/docs/integrations/webhooks/teams-webhooks)
- [DEV Community - Building a Microsoft Teams Bot App](https://dev.to/afrazkhan/building-a-microsoft-teams-bot-app-step-by-step-setup-guide-part-1-ndh)
