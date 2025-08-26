data "azurerm_client_config" "current" {}

resource "azuread_application" "this" {
  display_name = var.name
  # Enable implicit grants not required for workload identity; minimal config
}

resource "azuread_service_principal" "this" {
  client_id = azuread_application.this.client_id
}

# Optional password (disabled by default)
resource "azuread_service_principal_password" "this" {
  count                = var.create_password ? 1 : 0
  service_principal_id = azuread_service_principal.this.id
  end_date             = timeadd(timestamp(), "8760h")
}

# Federated identity credential on the AAD application
data "azuread_application" "app" {
  object_id = azuread_application.this.object_id
}

resource "azuread_application_federated_identity_credential" "fic" {
  application_id = azuread_application.this.id
  display_name   = "${var.service_account_namespace}-${var.service_account_name}"
  audiences      = var.audience
  issuer         = var.oidc_issuer_url
  subject        = "system:serviceaccount:${var.service_account_namespace}:${var.service_account_name}"
}
