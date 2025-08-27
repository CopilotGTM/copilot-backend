resource "azurerm_storage_account" "this" {
  name                     = var.storage_account_name
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = var.account_tier
  account_replication_type = var.account_replication_type

  dynamic "blob_properties" {
    for_each = length(var.allowed_blob_cors_origins) > 0 ? [1] : []
    content {
      cors_rule {
        allowed_headers    = ["*"]
        allowed_methods    = ["GET","HEAD","POST","PUT","DELETE","OPTIONS"]
        allowed_origins    = var.allowed_blob_cors_origins
        exposed_headers    = ["*"]
        max_age_in_seconds = 3600
      }
    }
  }

  tags = var.tags
}
