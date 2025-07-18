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
    # Remove application_stack block entirely
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

# -----------------------------------------------------------------
# Custom Domain Configuration
# -----------------------------------------------------------------

# DNS CNAME record pointing to the app service
resource "azurerm_dns_cname_record" "this" {
  count               = var.create_dns_records && var.custom_domain_name != null ? 1 : 0
  name                = split(".", var.custom_domain_name)[0]  # Extract subdomain
  zone_name           = var.dns_zone_name
  resource_group_name = var.dns_zone_resource_group
  ttl                 = 300
  record              = azurerm_linux_web_app.this.default_hostname
}

# Domain verification TXT record
resource "azurerm_dns_txt_record" "domain_verification" {
  count               = var.create_dns_records && var.custom_domain_name != null ? 1 : 0
  name                = "asuid.${split(".", var.custom_domain_name)[0]}"
  zone_name           = var.dns_zone_name
  resource_group_name = var.dns_zone_resource_group
  ttl                 = 300
  
  record {
    value = azurerm_linux_web_app.this.custom_domain_verification_id
  }
}

# Custom hostname binding
resource "azurerm_app_service_custom_hostname_binding" "this" {
  count               = var.custom_domain_name != null ? 1 : 0
  hostname            = var.custom_domain_name
  app_service_name    = azurerm_linux_web_app.this.name
  resource_group_name = var.resource_group_name

  depends_on = [
    azurerm_linux_web_app.this,
    azurerm_dns_cname_record.this,
    azurerm_dns_txt_record.domain_verification
  ]
}

# App Service Managed Certificate (Free SSL)
resource "azurerm_app_service_managed_certificate" "this" {
  count                      = var.custom_domain_name != null ? 1 : 0
  custom_hostname_binding_id = azurerm_app_service_custom_hostname_binding.this[0].id
  
  depends_on = [
    azurerm_app_service_custom_hostname_binding.this
  ]
}

# SSL Certificate binding
resource "azurerm_app_service_certificate_binding" "this" {
  count               = var.custom_domain_name != null ? 1 : 0
  hostname_binding_id = azurerm_app_service_custom_hostname_binding.this[0].id
  certificate_id      = azurerm_app_service_managed_certificate.this[0].id
  ssl_state           = "SniEnabled"

  depends_on = [
    azurerm_app_service_managed_certificate.this
  ]
}
