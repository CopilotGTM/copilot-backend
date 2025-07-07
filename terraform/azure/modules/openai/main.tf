resource "azurerm_cognitive_account" "this" {
  name                = var.account_name
  location            = var.location
  resource_group_name = var.resource_group_name
  kind                = "OpenAI"
  sku_name            = var.sku_name
  public_network_access_enabled = var.public_network_access_enabled

  tags = var.tags
}

resource "azurerm_cognitive_deployment" "this" {
  name                 = var.deployment_name
  cognitive_account_id = azurerm_cognitive_account.this.id
  model {
    format  = var.model_format
    name    = var.model_name
    # version = var.model_version
  }
  sku {
    name     = var.deployment_sku_name
    capacity = var.deployment_sku_capacity
  }
}