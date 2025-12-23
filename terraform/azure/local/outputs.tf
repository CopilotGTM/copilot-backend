output "resource_group_name" {
  value = module.resource_group.resource_group_name
}

output "key_vault_uri" {
  value = azurerm_key_vault.database_key_vault.vault_uri
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
  value = azurerm_cognitive_account.openai.endpoint
}

output "openai_key" {
  value     = nonsensitive(azurerm_cognitive_account.openai.primary_access_key)
  sensitive = false
}
