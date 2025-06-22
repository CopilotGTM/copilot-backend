output "acr_id" {
  description = "The ID of the Azure Container Registry."
  value       = azurerm_container_registry.this.id
}

output "acr_login_server" {
  description = "The login server URL of the ACR."
  value       = azurerm_container_registry.this.login_server
}

output "acr_admin_username" {
  description = "The admin username for the ACR (if admin_enabled)."
  value       = var.admin_enabled ? azurerm_container_registry.this.admin_username : null
}

output "acr_admin_password" {
  description = "The admin password for the ACR (if admin_enabled)."
  value       = var.admin_enabled ? azurerm_container_registry.this.admin_password : null
}