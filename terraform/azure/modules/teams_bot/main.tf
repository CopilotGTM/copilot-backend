# -----------------------------------------------------------------------------
# Data Sources
# -----------------------------------------------------------------------------
data "azurerm_client_config" "current" {}
data "azuread_client_config" "current" {}

# -----------------------------------------------------------------------------
# Microsoft Entra ID App Registration
# This creates the identity for the Teams bot
# -----------------------------------------------------------------------------
resource "azuread_application" "teams_bot" {
  display_name     = "${var.bot_display_name}-Bot-${title(var.env)}"
  sign_in_audience = "AzureADMultipleOrgs" # Multi-tenant to allow users from other orgs

  owners = [data.azuread_client_config.current.object_id]

  # Required redirect URI for Bot Framework authentication
  web {
    redirect_uris = [
      "https://token.botframework.com/.auth/web/redirect"
    ]
  }

  # Microsoft Graph API permissions
  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph API ID

    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d" # User.Read (delegated)
      type = "Scope"
    }

    resource_access {
      id   = "1c1b4c8e-3cc7-4c58-8470-9b92c9d5848b" # Chat.Read.WhereInstalled (application)
      type = "Role"
    }
  }

  tags = ["Teams", "Bot", var.env]
}

# -----------------------------------------------------------------------------
# Service Principal for the App Registration
# Required for Azure Bot Service to authenticate
# -----------------------------------------------------------------------------
resource "azuread_service_principal" "teams_bot" {
  client_id                    = azuread_application.teams_bot.client_id
  app_role_assignment_required = false
  owners                       = [data.azuread_client_config.current.object_id]
}

# -----------------------------------------------------------------------------
# Admin Consent: Chat.Read.WhereInstalled
# The app registration declares this permission (see required_resource_access above).
# Admin consent must be granted MANUALLY in Azure Portal → App Registrations →
# CopilotGTM-Bot → API Permissions → "Grant admin consent for [tenant]".
# Terraform's service principal lacks Global Admin privileges for this operation.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Client Secret (Password) for the Bot
# This is used by your backend to authenticate with Azure Bot Service
# -----------------------------------------------------------------------------
resource "azuread_application_password" "teams_bot" {
  application_id = azuread_application.teams_bot.id
  display_name   = "Teams Bot Secret - ${var.env}"
  end_date       = timeadd(timestamp(), "17520h") # 2 years (730 days * 24 hours)

  lifecycle {
    ignore_changes = [end_date]
  }
}

# -----------------------------------------------------------------------------
# Azure Bot Service Resource
# This is the "router" that connects Teams to your backend
# -----------------------------------------------------------------------------
resource "azurerm_bot_service_azure_bot" "teams_bot" {
  name                = "copilotgtm-bot-${var.env}"
  resource_group_name = var.resource_group_name
  location            = "global" # Azure Bot Service is a global resource

  microsoft_app_id        = azuread_application.teams_bot.client_id
  microsoft_app_type      = "SingleTenant"
  microsoft_app_tenant_id = data.azuread_client_config.current.tenant_id

  sku      = var.sku
  endpoint = var.messaging_endpoint

  tags = var.tags
}

# -----------------------------------------------------------------------------
# Enable Microsoft Teams Channel
# This allows your bot to communicate via Teams
# -----------------------------------------------------------------------------
resource "azurerm_bot_channel_ms_teams" "teams" {
  bot_name            = azurerm_bot_service_azure_bot.teams_bot.name
  resource_group_name = var.resource_group_name
  location            = azurerm_bot_service_azure_bot.teams_bot.location
}

# -----------------------------------------------------------------------------
# Store Bot Credentials in Key Vault
# Your backend will read these secrets to authenticate with Azure Bot Service
# -----------------------------------------------------------------------------
resource "azurerm_key_vault_secret" "bot_app_id" {
  name         = "teams-bot-app-id"
  value        = azuread_application.teams_bot.client_id
  key_vault_id = var.key_vault_id

  tags = var.tags
}

resource "azurerm_key_vault_secret" "bot_app_secret" {
  name         = "teams-bot-app-secret"
  value        = azuread_application_password.teams_bot.value
  key_vault_id = var.key_vault_id

  tags = var.tags
}

resource "azurerm_key_vault_secret" "bot_tenant_id" {
  name         = "teams-bot-tenant-id"
  value        = data.azuread_client_config.current.tenant_id
  key_vault_id = var.key_vault_id

  tags = var.tags
}
