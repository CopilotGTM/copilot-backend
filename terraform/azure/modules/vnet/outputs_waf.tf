output "waf_subnet_id" {
  description = "ID of the dedicated Application Gateway subnet (if created)."
  value       = try(azurerm_subnet.waf[0].id, null)
}
