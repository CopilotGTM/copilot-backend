locals {
  effective_public_network_access_enabled = var.private_endpoint_enabled ? false : var.public_network_access_enabled
  private_endpoint_name                   = "${var.storage_account_name}-pe"
}

resource "azurerm_storage_account" "this" {
  name                     = var.storage_account_name
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = var.account_tier
  account_replication_type = var.account_replication_type

  public_network_access_enabled   = local.effective_public_network_access_enabled
  https_traffic_only_enabled      = true
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = var.allow_nested_items_to_be_public
  shared_access_key_enabled       = var.shared_access_key_enabled

  blob_properties {
    versioning_enabled = var.blob_versioning_enabled

    dynamic "delete_retention_policy" {
      for_each = var.blob_delete_retention_days != null ? [1] : []
      content {
        days = var.blob_delete_retention_days
      }
    }

    dynamic "container_delete_retention_policy" {
      for_each = var.container_delete_retention_days != null ? [1] : []
      content {
        days = var.container_delete_retention_days
      }
    }

    dynamic "cors_rule" {
      for_each = length(var.allowed_blob_cors_origins) > 0 ? [1] : []
      content {
        allowed_headers    = ["*"]
        allowed_methods    = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"]
        allowed_origins    = var.allowed_blob_cors_origins
        exposed_headers    = ["*"]
        max_age_in_seconds = 3600
      }
    }
  }

  tags = var.tags
}

resource "azurerm_storage_management_policy" "this" {
  count = var.blob_versioning_enabled && var.blob_version_retention_days != null ? 1 : 0

  storage_account_id = azurerm_storage_account.this.id

  rule {
    name    = "delete-old-blob-versions"
    enabled = true

    filters {
      blob_types = ["blockBlob"]
    }

    actions {
      version {
        delete_after_days_since_creation = var.blob_version_retention_days
      }
    }
  }
}

resource "azurerm_monitor_diagnostic_setting" "blob_service" {
  count = var.blob_diagnostics_enabled ? 1 : 0

  name                       = "${var.storage_account_name}-blob-diagnostics"
  target_resource_id         = "${azurerm_storage_account.this.id}/blobServices/default"
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category = "StorageRead"
  }

  enabled_log {
    category = "StorageWrite"
  }

  enabled_log {
    category = "StorageDelete"
  }

  enabled_metric {
    category = "Transaction"
  }
}

resource "azurerm_private_endpoint" "this" {
  count               = var.private_endpoint_enabled ? 1 : 0
  name                = local.private_endpoint_name
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${local.private_endpoint_name}-psc"
    is_manual_connection           = false
    private_connection_resource_id = azurerm_storage_account.this.id
    subresource_names              = var.private_endpoint_subresource_names
  }

  dynamic "private_dns_zone_group" {
    for_each = var.private_dns_zone_ids != null ? [1] : []
    content {
      name                 = "default"
      private_dns_zone_ids = var.private_dns_zone_ids
    }
  }
}
