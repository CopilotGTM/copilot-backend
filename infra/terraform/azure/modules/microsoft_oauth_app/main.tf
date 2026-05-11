# -----------------------------------------------------------------------------
# CopilotGTM Microsoft OAuth App Registration
#
# Purpose-built for CopilotGTM's Outlook/Teams OAuth integrations.
# NOT a generic reusable module — the Microsoft Graph permissions are
# specific to CopilotGTM's sync features (email, calendar, Teams channels,
# chats, meetings, transcripts). Keep this in sync with backend Microsoft
# integration scopes.
#
# Creates a per-environment Azure AD app registration so each environment
# (local, dev, prod) has its own client ID + secret. This isolates:
# - Rate limits (Microsoft Graph quotas are per-app)
# - Consent screens (users see "CopilotGTM-Dev" vs "CopilotGTM-Prod")
# - Tokens (no cross-environment token conflicts)
#
# Current default scopes are delegated user-consent scopes. If future changes add
# admin-required Microsoft Graph permissions, document that feature separately and
# grant admin consent only for that expanded scope set.
# -----------------------------------------------------------------------------

data "azuread_client_config" "current" {}

# Microsoft Graph API well-known app ID
locals {
  ms_graph_app_id = "00000003-0000-0000-c000-000000000000"
}

resource "azuread_application" "copilotgtm" {
  display_name     = "${var.display_name} MS-Integration - ${title(var.env)}"
  sign_in_audience = "AzureADMultipleOrgs"

  owners = [data.azuread_client_config.current.object_id]

  web {
    redirect_uris = var.redirect_uris
  }

  # ---------------------------------------------------------------------------
  # Microsoft Graph API Permissions
  # ---------------------------------------------------------------------------
  required_resource_access {
    resource_app_id = local.ms_graph_app_id

    # ── Delegated permissions (type = "Scope") ──

    # Calendars.Read — Read user calendars
    resource_access {
      id   = "465a38f9-76ea-45b9-9f34-9e8b0d4b0b42"
      type = "Scope"
    }

    # Channel.ReadBasic.All — Read channel names and descriptions
    resource_access {
      id   = "9d8982ae-4365-4f57-95e9-d6032a4c0b87"
      type = "Scope"
    }

    # Chat.Read — Read user chat messages
    resource_access {
      id   = "f501c180-9344-439a-bca0-6cbf209fd270"
      type = "Scope"
    }

    # Mail.Read — Read user mail
    resource_access {
      id   = "570282fd-fa5c-430d-a7fd-fc8dc98a9dca"
      type = "Scope"
    }

    # offline_access — Maintain access (refresh tokens)
    resource_access {
      id   = "7427e0e9-2fba-42fe-b0c0-848c9e6a8182"
      type = "Scope"
    }

    # OnlineMeetings.Read — Read user online meetings
    resource_access {
      id   = "9be106e1-f4e3-4df5-bdff-e4bc531cbe43"
      type = "Scope"
    }

    # Team.ReadBasic.All — Read team names and descriptions
    resource_access {
      id   = "485be79e-c497-4b35-9400-0e3fa7f2a5d4"
      type = "Scope"
    }

    # User.Read — Sign in and read user profile
    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"
      type = "Scope"
    }

  }

  tags = ["CopilotGTM", "OAuth", var.env]
}

# -----------------------------------------------------------------------------
# Service Principal — required for admin consent and token issuance
# -----------------------------------------------------------------------------
resource "azuread_service_principal" "copilotgtm" {
  client_id                    = azuread_application.copilotgtm.client_id
  app_role_assignment_required = false
  owners                       = [data.azuread_client_config.current.object_id]
}

# -----------------------------------------------------------------------------
# Client Secret — stored in Key Vault, 2-year rotation
# -----------------------------------------------------------------------------
resource "azuread_application_password" "copilotgtm" {
  application_id = azuread_application.copilotgtm.id
  display_name   = "CopilotGTM OAuth Secret - ${var.env}"
  end_date       = timeadd(timestamp(), "17520h") # 2 years

  lifecycle {
    ignore_changes = [end_date]
  }
}

# -----------------------------------------------------------------------------
# Store credentials in Key Vault
# Uses the same secret names the backend reads from:
#   - microsoft-integration-client-id
#   - microsoft-integration-client-secret
# -----------------------------------------------------------------------------
resource "azurerm_key_vault_secret" "microsoft_client_id" {
  name         = "microsoft-integration-client-id"
  value        = azuread_application.copilotgtm.client_id
  key_vault_id = var.key_vault_id

  tags = var.tags
}

resource "azurerm_key_vault_secret" "microsoft_client_secret" {
  name         = "microsoft-integration-client-secret"
  value        = azuread_application_password.copilotgtm.value
  key_vault_id = var.key_vault_id

  tags = var.tags
}
