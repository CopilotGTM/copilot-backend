output "resource_group_name" {
  value = module.resource_group.resource_group_name
}

output "master_key_vault_uri" {
  value = azurerm_key_vault.master_key_vault.vault_uri
}

output "storage_account_name" {
  value = azurerm_storage_account.master_storage_account.name
}

output "org_storage_account_name" {
  value = azurerm_storage_account.org_storage_account.name
}

output "database_host" {
  value = azurerm_postgresql_flexible_server.db.fqdn
}

output "database_admin_user" {
  value = "CopilotGTMAdmin"
}

output "database_password" {
  value     = nonsensitive(random_password.db_admin.result)
  sensitive = false # Intentionally printed for local/dev convenience
}

# Azure OpenAI outputs
output "openai_endpoint" {
  value = module.azure_openai.openai_endpoint
}

output "openai_key" {
  value     = nonsensitive(module.azure_openai.primary_key)
  sensitive = false
}

# Teams Bot outputs
output "teams_bot_app_id" {
  value = module.teams_bot.bot_app_id
}

output "teams_bot_name" {
  value = module.teams_bot.bot_name
}
