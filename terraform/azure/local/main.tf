# -----------------------------------------------------------------
# Resource Group
# -----------------------------------------------------------------
module "resource_group" {
  source              = "../modules/resource_group"
  resource_group_name = "${var.resource_group_name}-${var.env}"
  location            = var.location
}

locals {
  # Mirrors dev storage CORS pattern; defaults to localhost for local dev.
  allowed_blob_cors_origins = ["http://localhost:3000"]
}

# -----------------------------------------------------------------
# Key Vault (Public)
# -----------------------------------------------------------------
data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "database_key_vault" {
  name                        = "app-database-vault-${var.env}"
  location                    = var.location
  resource_group_name         = module.resource_group.resource_group_name
  enabled_for_disk_encryption = true
  tenant_id                   = data.azurerm_client_config.current.tenant_id
  soft_delete_retention_days  = 7
  purge_protection_enabled    = false
  sku_name                    = "standard"

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    key_permissions = [
      "Get", "List", "Create", "Delete", "Update", "Purge", "Recover"
    ]

    secret_permissions = [
      "Get", "List", "Set", "Delete", "Purge", "Recover"
    ]

    storage_permissions = [
      "Get", "List", "Delete", "Set", "Update", "Purge", "Recover"
    ]
  }

  public_network_access_enabled = true

  network_acls {
    bypass         = "AzureServices"
    default_action = "Allow"
    ip_rules       = []
  }

  tags = {
    environment = var.env
    project     = var.project_name
  }
}

# -----------------------------------------------------------------
# Master Key Vault (Public) - mirrors dev's master_key_vault
# -----------------------------------------------------------------
resource "azurerm_key_vault" "master_key_vault" {
  name                        = "app-master-vault-${var.env}"
  location                    = var.location
  resource_group_name         = module.resource_group.resource_group_name
  enabled_for_disk_encryption = true
  tenant_id                   = data.azurerm_client_config.current.tenant_id
  soft_delete_retention_days  = 7
  purge_protection_enabled    = false
  sku_name                    = "standard"

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    key_permissions = [
      "Get", "List", "Create", "Delete", "Update", "Purge", "Recover"
    ]

    secret_permissions = [
      "Get", "List", "Set", "Delete", "Purge", "Recover"
    ]

    storage_permissions = [
      "Get", "List", "Delete", "Set", "Update", "Purge", "Recover"
    ]
  }

  public_network_access_enabled = true

  network_acls {
    bypass         = "AzureServices"
    default_action = "Allow"
    ip_rules       = []
  }

  tags = {
    environment = var.env
    project     = var.project_name
  }
}

# -----------------------------------------------------------------
# Storage Accounts (Public)
# Note: defined directly (no module) to avoid any Private Link behavior.
# -----------------------------------------------------------------

moved {
  from = module.storage_account.azurerm_storage_account.this
  to   = azurerm_storage_account.master_storage_account
}

moved {
  from = module.org_storage_account.azurerm_storage_account.this
  to   = azurerm_storage_account.org_storage_account
}

resource "azurerm_storage_account" "master_storage_account" {
  name                     = "masterstorage${var.env}"
  resource_group_name      = module.resource_group.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"

  public_network_access_enabled = true

  network_rules {
    default_action = "Allow"
    bypass         = ["AzureServices"]
    ip_rules       = []
  }

  dynamic "blob_properties" {
    for_each = length(local.allowed_blob_cors_origins) > 0 ? [1] : []
    content {
      cors_rule {
        allowed_headers    = ["*"]
        allowed_methods    = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"]
        allowed_origins    = local.allowed_blob_cors_origins
        exposed_headers    = ["*"]
        max_age_in_seconds = 3600
      }
    }
  }

  tags = {
    environment = var.env
    project     = var.project_name
  }
}

resource "azurerm_storage_account" "org_storage_account" {
  name                     = "organisationstorage${var.env}"
  resource_group_name      = module.resource_group.resource_group_name
  network_rules {
    default_action = "Allow"
    bypass         = ["AzureServices"]
    ip_rules       = []
  }

  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"

  public_network_access_enabled = true

  dynamic "blob_properties" {
    for_each = length(local.allowed_blob_cors_origins) > 0 ? [1] : []
    content {
      cors_rule {
        allowed_headers    = ["*"]
        allowed_methods    = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"]
        allowed_origins    = local.allowed_blob_cors_origins
        exposed_headers    = ["*"]
        max_age_in_seconds = 3600
      }
    }
  }

  tags = {
    environment = var.env
    project     = var.project_name
    purpose     = "organisation-data"
  }
}

resource "azurerm_storage_container" "master" {
  name                  = "master"
  storage_account_id    = azurerm_storage_account.master_storage_account.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "organisation" {
  name                  = "organisation"
  storage_account_id    = azurerm_storage_account.org_storage_account.id
  container_access_type = "private"
}

# -----------------------------------------------------------------
# Random Password for DB
# -----------------------------------------------------------------
resource "random_password" "db_admin" {
  length           = 32
  override_special = "!@#$%&*()-_=+[]"
  special          = true
  upper            = true
  lower            = true
  numeric          = true
}

# -----------------------------------------------------------------
# PostgreSQL Flexible Server (Public)
# -----------------------------------------------------------------
moved {
  from = module.application_database.azurerm_key_vault_secret.db_password
  to   = azurerm_key_vault_secret.db_password
}

moved {
  from = module.application_database.azurerm_postgresql_flexible_server.db
  to   = azurerm_postgresql_flexible_server.db
}

moved {
  from = module.application_database.azurerm_postgresql_flexible_server_configuration.vector_extension
  to   = azurerm_postgresql_flexible_server_configuration.vector_extension
}

moved {
  from = module.application_database.azurerm_postgresql_flexible_server_firewall_rule.allow_all
  to   = azurerm_postgresql_flexible_server_firewall_rule.allow_all
}

resource "azurerm_key_vault_secret" "db_password" {
  name         = "db-admin-password"
  value        = random_password.db_admin.result
  key_vault_id = azurerm_key_vault.database_key_vault.id
}

resource "azurerm_postgresql_flexible_server" "db" {
  name                          = "${var.db_name}-${var.env}"
  resource_group_name           = module.resource_group.resource_group_name
  location                      = var.db_location
  version                       = "14"
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 65536
  backup_retention_days         = 7
  public_network_access_enabled = true

  # Pin zone to avoid provider attempting to unset it on existing servers.
  zone = "1"

  administrator_login    = "CopilotGTMAdmin"
  administrator_password = azurerm_key_vault_secret.db_password.value

  tags = {
    environment = var.env
    project     = var.project_name
  }
}

resource "azurerm_postgresql_flexible_server_configuration" "vector_extension" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.db.id
  value     = "VECTOR"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_all" {
  name             = "allow-all-ips"
  server_id        = azurerm_postgresql_flexible_server.db.id
  start_ip_address = var.public_ip_range_start
  end_ip_address   = var.public_ip_range_end
}

# -----------------------------------------------------------------
# Azure OpenAI (uses shared module, same pattern as dev/prod)
# -----------------------------------------------------------------
module "azure_openai" {
  source            = "../modules/azure_openai"
  prefix            = var.project_name
  env               = var.env
  resource_group_id = module.resource_group.resource_group_id
  location          = "eastus2" # OpenAI models are available in East US 2

  # Define all models to deploy - add/remove models as needed
  models = {
    gpt5 = {
      deployment_name = "gpt-5"
      model_name      = "gpt-5"
      sku             = "GlobalStandard"
      capacity        = 100
      version         = "2025-08-07"
    }
    gpt5_mini = {
      deployment_name = "gpt-5-mini"
      model_name      = "gpt-5-mini"
      sku             = "GlobalStandard"
      capacity        = 100
      version         = "2025-08-07"
    }
    gpt5_2 = {
      deployment_name = "gpt-5.2"
      model_name      = "gpt-5.2"
      sku             = "GlobalStandard"
      capacity        = 100
      version         = "2025-12-11"
    }
    gpt5_4 = {
      deployment_name = "gpt-5.4"
      model_name      = "gpt-5.4"
      sku             = "GlobalStandard"
      capacity        = 1000
      version         = "2026-03-05"
    }
    transcribe = {
      deployment_name = "gpt-4o-transcribe-diarize"
      model_name      = "gpt-4o-transcribe-diarize"
      sku             = "GlobalStandard"
      capacity        = 1
      version         = "2025-10-15"
    }
    gpt4o_transcribe = {
      deployment_name = "gpt-4o-transcribe"
      model_name      = "gpt-4o-transcribe"
      sku             = "GlobalStandard"
      capacity        = 30
      version         = "2025-03-20"
    }
    embedder = {
      deployment_name = "text-embedding-3-large"
      model_name      = "text-embedding-3-large"
      sku             = "Standard"
      capacity        = 1
      version         = "1"
      publisher       = "OpenAI"
    }
  }

  tags = {
    environment = var.env
    project     = var.project_name
  }

  depends_on = [module.resource_group]
}

# -----------------------------------------------------------------
# Teams Bot (Free tier – F0)
# -----------------------------------------------------------------
module "teams_bot" {
  source = "../modules/teams_bot"

  env                 = var.env
  bot_display_name    = "CopilotGTM"
  resource_group_name = module.resource_group.resource_group_name
  key_vault_id        = azurerm_key_vault.master_key_vault.id

  messaging_endpoint = var.teams_bot_messaging_endpoint

  # F0 = Free tier (10K messages/month)
  sku = "F0"

  tags = {
    environment = var.env
    project     = var.project_name
  }

  depends_on = [
    module.resource_group,
    azurerm_key_vault.master_key_vault
  ]
}
