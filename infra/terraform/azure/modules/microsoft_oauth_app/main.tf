# -----------------------------------------------------------------------------
# CopilotGTM Microsoft OAuth App Registration
#
# Purpose-built for CopilotGTM's Outlook/Teams OAuth integrations.
# NOT a generic reusable module — the 24 Microsoft Graph permissions are
# specific to CopilotGTM's sync features (email, calendar, Teams channels,
# chats, meetings, transcripts).
#
# Creates a per-environment Azure AD app registration so each environment
# (local, dev, prod) has its own client ID + secret. This isolates:
# - Rate limits (Microsoft Graph quotas are per-app)
# - Consent screens (users see "CopilotGTM-Dev" vs "CopilotGTM-Prod")
# - Tokens (no cross-environment token conflicts)
#
# After terraform apply, admin consent must be granted manually:
#   Azure Portal → App Registrations → CopilotGTM-{Env} → API Permissions
#   → "Grant admin consent for {tenant}"
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
  # Microsoft Graph API Permissions (24 total — matches production app)
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

    # ChannelMessage.Read.All — Read user channel messages
    resource_access {
      id   = "767156cb-16ae-4d10-8f8b-41b657c8c8c8"
      type = "Scope"
    }

    # ChannelSettings.Read.All — Read channel settings
    resource_access {
      id   = "233e0cf1-dd62-48bc-b65b-b38fe87fcf8e"
      type = "Scope"
    }

    # ChannelSettings.ReadWrite.All — Read and write channel settings
    resource_access {
      id   = "d649fb7c-72b4-4eec-b2b4-b15acf79e378"
      type = "Scope"
    }

    # Chat.Read — Read user chat messages
    resource_access {
      id   = "f501c180-9344-439a-bca0-6cbf209fd270"
      type = "Scope"
    }

    # Directory.Read.All — Read directory data (delegated)
    resource_access {
      id   = "06da0dbc-49e2-44d2-8312-53f166ab848a"
      type = "Scope"
    }

    # Directory.ReadWrite.All — Read and write directory data
    resource_access {
      id   = "c5366453-9fb0-48a5-a156-24f0c49a4b84"
      type = "Scope"
    }

    # Group.Read.All — Read all groups (delegated)
    resource_access {
      id   = "5f8c59db-677d-491f-a6b8-5f174b11ec1d"
      type = "Scope"
    }

    # Group.ReadWrite.All — Read and write all groups
    resource_access {
      id   = "4e46008b-f24c-477d-8fff-7bb4ec7aafe0"
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

    # OnlineMeetingTranscript.Read.All — Read all transcripts of online meetings
    resource_access {
      id   = "30b87d18-ebb1-45db-97f8-82ccb1f0190c"
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

    # User.Read.All — Read all users' full profiles (delegated)
    resource_access {
      id   = "a154be20-db9c-4678-8ab7-66f6cc099a59"
      type = "Scope"
    }

    # ── Application permissions (type = "Role") ──

    # Calls.AccessMedia.All — Access media streams in a call as an app
    resource_access {
      id   = "a7a681dc-756e-4909-b988-f160edc6655f"
      type = "Role"
    }

    # Calls.JoinGroupCall.All — Join group calls and meetings as an app
    resource_access {
      id   = "f6b49018-60ab-4f81-83bd-22caeabfed2d"
      type = "Role"
    }

    # Calls.JoinGroupCallAsGuest.All — Join group calls and meetings as a guest
    resource_access {
      id   = "fd7ccf6b-3d28-418b-9701-cd10f5cd2fd4"
      type = "Role"
    }

    # Directory.Read.All — Read directory data (application)
    resource_access {
      id   = "7ab1d382-f21e-4acd-a863-ba3e13f7da61"
      type = "Role"
    }

    # Group.Read.All — Read all groups (application)
    resource_access {
      id   = "5b567255-7703-4780-807c-7be8301ae99b"
      type = "Role"
    }

    # OnlineMeetings.Read.All — Read online meeting details (application)
    resource_access {
      id   = "c1684f21-1984-47fa-9d61-2dc8c296bb70"
      type = "Role"
    }

    # User.Read.All — Read all users' full profiles (application)
    resource_access {
      id   = "df021288-bdef-4463-88db-98f22de89214"
      type = "Role"
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
