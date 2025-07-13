output "frontdoor_endpoint_hostname" {
  description = "The hostname of the Front Door endpoint"
  value       = azurerm_cdn_frontdoor_endpoint.main.host_name
}