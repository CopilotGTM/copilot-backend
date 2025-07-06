output "acr_id" {
  description = "The ID of the Azure Container Registry."
  value       = azurerm_container_registry.this.id
}

output "acr_login_server" {
  description = "The login server URL of the ACR."
  value       = azurerm_container_registry.this.login_server
}
