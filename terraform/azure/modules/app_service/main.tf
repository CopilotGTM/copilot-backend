resource "azurerm_service_plan" "this" {
  name                = var.app_service_plan_name
  location            = var.location
  resource_group_name = var.resource_group_name

  sku_name = var.sku_name
  os_type  = var.os_type
}

resource "azurerm_linux_web_app" "this" {
  name                = var.app_service_name
  location            = var.location
  resource_group_name = var.resource_group_name
  service_plan_id     = azurerm_service_plan.this.id

  identity {
    type = "SystemAssigned"
  }

  site_config {
    application_stack {
      node_version = var.node_version
    }
  }

  https_only = var.https_only

  app_settings = var.app_settings
}