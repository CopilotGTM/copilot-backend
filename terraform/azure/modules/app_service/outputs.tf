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

output "app_service_principal_id" {
  description = "The ID of the managed identity principal"
  value       = azurerm_linux_web_app.this.identity[0].principal_id
}

output "app_service_outbound_ip_addresses" {
  description = "The outbound IP addresses of the Linux Web App"
  value       = azurerm_linux_web_app.this.outbound_ip_address_list
}

output "app_service_name" {
  description = "The name of the Linux Web App"
  value       = azurerm_linux_web_app.this.name
}

output "app_service_custom_domain_verification_id" {
  description = "The custom domain verification ID for the App Service"
  value       = azurerm_linux_web_app.this.custom_domain_verification_id
}

output "custom_domain_url" {
  description = "The custom domain URL if configured"
  value       = var.custom_domain_name != null ? "https://${var.custom_domain_name}" : null
}

output "managed_certificate_id" {
  description = "The ID of the managed certificate"
  value       = var.custom_domain_name != null ? azurerm_app_service_managed_certificate.this[0].id : null
}
