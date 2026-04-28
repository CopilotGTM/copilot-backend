output "service_connection_id" {
  description = "The ID of the Azure DevOps service connection."
  value       = var.use_workload_identity_federation ? azuredevops_serviceendpoint_azurerm.oidc[0].id : azuredevops_serviceendpoint_azurerm.secret[0].id
}

output "service_connection_name" {
  description = "The name of the Azure DevOps service connection."
  value       = var.service_connection_name
}

output "subscription_id" {
  description = "Azure Subscription ID for this service connection."
  value       = var.subscription_id
}

output "workload_identity_federation_issuer" {
  description = "The OIDC issuer URL for the service connection (only when using WorkloadIdentityFederation)."
  value       = var.use_workload_identity_federation ? azuredevops_serviceendpoint_azurerm.oidc[0].workload_identity_federation_issuer : ""
}

output "workload_identity_federation_subject" {
  description = "The OIDC subject for the service connection (only when using WorkloadIdentityFederation)."
  value       = var.use_workload_identity_federation ? azuredevops_serviceendpoint_azurerm.oidc[0].workload_identity_federation_subject : ""
}

output "uses_oidc" {
  description = "Whether the service connection uses Workload Identity Federation (OIDC)."
  value       = var.use_workload_identity_federation
}
