output "client_id" {
  description = "The Application (Client) ID of the service principal."
  value       = azuread_application.app.client_id
}

output "client_secret" {
  description = "The client secret for the service principal (empty if using OIDC)."
  value       = var.create_password ? azuread_service_principal_password.spp[0].value : ""
  sensitive   = true
}

output "tenant_id" {
  description = "The Tenant ID of the service principal."
  value       = data.azurerm_client_config.current.tenant_id
}

output "object_id" {
  description = "The Object ID of the service principal."
  value       = azuread_service_principal.sp.object_id
}

output "application_id" {
  description = "The Application (object) ID of the app registration (used for federated credentials)."
  value       = azuread_application.app.id
}
