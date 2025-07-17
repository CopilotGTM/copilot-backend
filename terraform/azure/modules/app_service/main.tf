terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 3.0"
    }
    azapi = {
      source = "azure/azapi"
    }
  }
}

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
    app_command_line = ""
    application_stack {
      docker_image_name   = var.docker_image_name
      docker_registry_url = "https://${var.docker_registry_url}"
    }
    container_registry_use_managed_identity = true
  }

  https_only = var.https_only

  # Only include essential app settings - remove DOCKER_REGISTRY_SERVER_URL
  app_settings = merge({
    "WEBSITES_ENABLE_APP_SERVICE_STORAGE" = "false"
  }, var.app_settings)

  depends_on = [
    azurerm_service_plan.this
  ]
}

# Role Assignment: Allow App Service to pull images from ACR
resource "azurerm_role_assignment" "app_service_acr_pull" {
  principal_id         = azurerm_linux_web_app.this.identity[0].principal_id
  role_definition_name = "AcrPull"
  scope                = var.acr_id
  principal_type       = "ServicePrincipal"
  
  depends_on = [
    azurerm_linux_web_app.this
  ]
}
