resource "azuredevops_serviceendpoint_azurerm" "service_connection" {
  project_id            = var.devops_project_id
  service_endpoint_name = var.service_connection_name
  description           = "Service connection for ACR and AKS access"

  azurerm_spn_tenantid      = var.tenant_id
  azurerm_subscription_id   = var.subscription_id
  azurerm_subscription_name = var.subscription_name

  credentials {
    serviceprincipalid  = var.client_id
    serviceprincipalkey = var.client_secret
  }

}
