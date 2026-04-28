output "bot_app_id" {
  description = "The Application (Client) ID of the Teams bot"
  value       = azuread_application.teams_bot.client_id
}

output "bot_app_object_id" {
  description = "The Object ID of the Teams bot application"
  value       = azuread_application.teams_bot.object_id
}

output "bot_tenant_id" {
  description = "The Tenant ID where the bot is registered"
  value       = data.azuread_client_config.current.tenant_id
}

output "bot_app_secret" {
  description = "The client secret for the Teams bot (sensitive)"
  value       = azuread_application_password.teams_bot.value
  sensitive   = true
}

output "bot_name" {
  description = "The name of the Azure Bot resource"
  value       = azurerm_bot_service_azure_bot.teams_bot.name
}

output "bot_endpoint" {
  description = "The messaging endpoint configured for the bot"
  value       = azurerm_bot_service_azure_bot.teams_bot.endpoint
}

output "service_principal_object_id" {
  description = "The Object ID of the bot's service principal"
  value       = azuread_service_principal.teams_bot.object_id
}

# Key Vault secret names (for reference in backend config)
output "key_vault_secret_names" {
  description = "Names of the secrets stored in Key Vault"
  value = {
    app_id     = azurerm_key_vault_secret.bot_app_id.name
    app_secret = azurerm_key_vault_secret.bot_app_secret.name
    tenant_id  = azurerm_key_vault_secret.bot_tenant_id.name
  }
}
