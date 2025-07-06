output "acr_service_connection_id" {
  description = "The ID of the Azure DevOps Docker Registry (ACR) service connection."
  value       = azuredevops_serviceendpoint_dockerregistry.acr.id
}

output "acr_service_connection_name" {
  description = "The name of the Azure DevOps Docker Registry (ACR) service connection."
  value       = azuredevops_serviceendpoint_dockerregistry.acr.service_endpoint_name
}