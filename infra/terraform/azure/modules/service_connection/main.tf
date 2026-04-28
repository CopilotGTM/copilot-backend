# -----------------------------------------------------------------------------
# Azure DevOps Service Connection with Workload Identity Federation (OIDC)
#
# When use_workload_identity_federation = true (default):
#   - Uses OIDC tokens instead of client secrets
#   - No secrets to rotate
#   - Requires federated credential on the app registration
#
# When use_workload_identity_federation = false:
#   - Uses traditional client_id + client_secret
#   - Requires secret rotation (typically yearly)
# -----------------------------------------------------------------------------

# OIDC-based service connection (no secret)
resource "azuredevops_serviceendpoint_azurerm" "oidc" {
  count = var.use_workload_identity_federation ? 1 : 0

  project_id                             = var.devops_project_id
  service_endpoint_name                  = var.service_connection_name
  description                            = "OIDC service connection for Azure (no secrets)"
  service_endpoint_authentication_scheme = "WorkloadIdentityFederation"

  azurerm_spn_tenantid      = var.tenant_id
  azurerm_subscription_id   = var.subscription_id
  azurerm_subscription_name = var.subscription_name

  credentials {
    serviceprincipalid = var.client_id
  }
}

# Secret-based service connection (legacy)
resource "azuredevops_serviceendpoint_azurerm" "secret" {
  count = var.use_workload_identity_federation ? 0 : 1

  project_id            = var.devops_project_id
  service_endpoint_name = var.service_connection_name
  description           = "Service connection for Azure (secret-based)"

  azurerm_spn_tenantid      = var.tenant_id
  azurerm_subscription_id   = var.subscription_id
  azurerm_subscription_name = var.subscription_name

  credentials {
    serviceprincipalid  = var.client_id
    serviceprincipalkey = var.client_secret
  }
}
