output "app_service_id" {
  description = "The ID of the Linux Web App"
  value       = azurerm_linux_web_app.this.id
}

output "app_service_default_hostname" {
  description = "The default hostname of the Linux Web App"
  value       = azurerm_linux_web_app.this.default_hostname
}

output "app_service_plan_id" {
  description = "The ID of the Service Plan"
  value       = azurerm_service_plan.this.id
}