# -----------------------------------------------------------------------------
# Azure DevOps Service Connection for Azure Container Registry (ACR)
#
# Uses Azure Container Registry service endpoint with Workload Identity
# Federation (OIDC) - no secrets to rotate!
#
# This module uses OIDC by default. The same service principal used for the
# main Azure RM service connection can authenticate to ACR without secrets.
# -----------------------------------------------------------------------------

resource "azuredevops_serviceendpoint_azurecr" "this" {
  project_id                             = var.devops_project_id
  service_endpoint_name                  = var.service_endpoint_name
  service_endpoint_authentication_scheme = "WorkloadIdentityFederation"
  resource_group                         = var.resource_group_name
  azurecr_name                           = var.acr_name
  azurecr_subscription_id                = var.subscription_id
  azurecr_subscription_name              = var.subscription_name
  azurecr_spn_tenantid                   = var.tenant_id

  credentials {
    serviceprincipalid = var.client_id
  }
}
