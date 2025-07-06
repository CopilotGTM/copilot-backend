output "service_connection_id" {
  description = "Azure DevOps Service Connection ID."
  value       = azuredevops_serviceendpoint_azurerm.service_connection.id
}

output "service_connection_name" {
  description = "Azure DevOps Service Connection name."
  value       = azuredevops_serviceendpoint_azurerm.service_connection.service_endpoint_name
}

output "subscription_id" {
  description = "Azure Subscription ID for this service connection."
  value       = azuredevops_serviceendpoint_azurerm.service_connection.azurerm_subscription_id
}