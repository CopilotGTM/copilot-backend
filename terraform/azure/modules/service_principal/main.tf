data "azurerm_client_config" "current" {}

resource "azuread_application" "app" {
  display_name = var.sp_name

  # Microsoft Graph API permissions
  # These GUIDs are Microsoft's static permission IDs (same for all Azure tenants)
  # Reference: https://learn.microsoft.com/en-us/graph/permissions-reference
  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph API ID (static)

    resource_access {
      # Permission: Application.ReadWrite.OwnedBy
      # Allows the app to manage apps it owns
      id   = "18a4783c-866b-4cc7-a460-3d5e5662c884"
      type = "Role"
    }

    resource_access {
      # Permission: Application.ReadWrite.All  
      # Allows the app to manage all app registrations (needed for federated creds)
      id   = "1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9"
      type = "Role"
    }
  }
}

resource "azuread_service_principal" "sp" {
  client_id = azuread_application.app.client_id
}

resource "azuread_service_principal_password" "spp" {
  service_principal_id = azuread_service_principal.sp.id
  end_date             = timeadd(timestamp(), "8760h")
}

resource "azurerm_role_assignment" "sp_role" {
  scope                = "/subscriptions/${var.subscription_id}"
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.sp.object_id
}

# -----------------------------------------------------------------------------
# Auto-grant Admin Consent (runs during bootstrap when admin is logged in)
# -----------------------------------------------------------------------------
resource "null_resource" "grant_admin_consent" {
  depends_on = [azuread_application.app]

  provisioner "local-exec" {
    command = "az ad app permission admin-consent --id ${azuread_application.app.client_id}"
  }

  # Re-run if the app changes
  triggers = {
    app_id = azuread_application.app.client_id
  }
}
