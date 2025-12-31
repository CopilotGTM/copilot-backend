output "acr_service_connection_id" {
  description = "The ID of the Azure DevOps ACR service connection."
  value       = azuredevops_serviceendpoint_azurecr.this.id
}

output "acr_service_connection_name" {
  description = "The name of the Azure DevOps ACR service connection."
  value       = azuredevops_serviceendpoint_azurecr.this.service_endpoint_name
}

output "workload_identity_federation_issuer" {
  description = "The OIDC issuer URL for the ACR service connection."
  value       = azuredevops_serviceendpoint_azurecr.this.workload_identity_federation_issuer
}

output "workload_identity_federation_subject" {
  description = "The OIDC subject for the ACR service connection."
  value       = azuredevops_serviceendpoint_azurecr.this.workload_identity_federation_subject
}
