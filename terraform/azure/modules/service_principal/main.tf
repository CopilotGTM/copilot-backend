data "azurerm_client_config" "current" {}

resource "azuread_application" "app" {
  display_name = var.sp_name
}

resource "azuread_service_principal" "sp" {
  client_id = azuread_application.app.client_id
}

resource "azuread_service_principal_password" "spp" {
  service_principal_id = azuread_service_principal.sp.id
  # Set password to expire in 1 year from apply
  end_date = timeadd(timestamp(), "8760h")
}

resource "azurerm_role_assignment" "sp_role" {
  scope                = "/subscriptions/${var.subscription_id}"
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.sp.object_id
}