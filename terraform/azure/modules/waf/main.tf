resource "azurerm_public_ip" "waf" {
  name                = "${var.waf_name}-pip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = var.zones

  tags = var.tags
}

resource "azurerm_application_gateway" "waf" {
  name                = var.waf_name
  resource_group_name = var.resource_group_name
  location            = var.location
  zones               = var.zones

  sku {
    name = var.appgw_sku
    tier = var.appgw_sku
  }

  autoscale_configuration {
    min_capacity = var.min_capacity
    max_capacity = var.max_capacity
  }

  gateway_ip_configuration {
    name      = "appGatewayIpConfig"
    subnet_id = var.subnet_id
  }

  frontend_port {
    name = "port_80"
    port = 80
  }

  frontend_ip_configuration {
    name                 = "appGatewayFrontendIP"
    public_ip_address_id = azurerm_public_ip.waf.id
  }

  backend_address_pool {
    name = "default-backend-pool"
  }

  backend_http_settings {
    name                  = "default-http-settings"
    cookie_based_affinity = "Disabled"
    port                  = 80
    protocol              = "Http"
    request_timeout       = 60
  }

  http_listener {
    name                           = "default-listener"
    frontend_ip_configuration_name = "appGatewayFrontendIP"
    frontend_port_name             = "port_80"
    protocol                       = "Http"
  }

  request_routing_rule {
    name                       = "default-routing-rule"
    rule_type                  = "Basic"
    http_listener_name         = "default-listener"
    backend_address_pool_name  = "default-backend-pool"
    backend_http_settings_name = "default-http-settings"
    priority                   = 100
  }

  # WAF configuration - only enabled when SKU is WAF_v2
  dynamic "waf_configuration" {
    for_each = var.appgw_sku == "WAF_v2" ? [1] : []
    content {
      enabled          = true
      firewall_mode    = var.waf_mode
      rule_set_type    = "OWASP"
      rule_set_version = "3.2"
    }
  }

  tags = var.tags

  # Ignore changes made by AGIC (Application Gateway Ingress Controller)
  lifecycle {
    ignore_changes = [
      backend_address_pool,
      backend_http_settings,
      http_listener,
      request_routing_rule,
      probe,
      frontend_port,
      ssl_certificate,
      redirect_configuration,
      url_path_map
    ]
  }
}

# -----------------------------------------------------------------
# Log Analytics Workspace for WAF diagnostics
# Only created when WAF_v2 SKU is used (Standard_v2 has no firewall logs)
# -----------------------------------------------------------------
resource "azurerm_log_analytics_workspace" "waf_logs" {
  count               = var.enable_diagnostics && var.appgw_sku == "WAF_v2" ? 1 : 0
  name                = "${var.waf_name}-logs"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days

  tags = var.tags
}

# -----------------------------------------------------------------
# WAF Diagnostic Settings - enables firewall and access logs
# Only created when WAF_v2 SKU is used
# -----------------------------------------------------------------
resource "azurerm_monitor_diagnostic_setting" "waf_diagnostics" {
  count                      = var.enable_diagnostics && var.appgw_sku == "WAF_v2" ? 1 : 0
  name                       = "${var.waf_name}-diagnostics"
  target_resource_id         = azurerm_application_gateway.waf.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.waf_logs[0].id

  enabled_log {
    category = "ApplicationGatewayFirewallLog"
  }

  enabled_log {
    category = "ApplicationGatewayAccessLog"
  }
}
