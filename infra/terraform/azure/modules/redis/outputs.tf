output "hostname" {
  description = "The hostname of the Redis cache."
  value       = azurerm_redis_cache.this.hostname
}

output "primary_access_key" {
  description = "The primary access key for the Redis cache."
  value       = azurerm_redis_cache.this.primary_access_key
  sensitive   = true
}

output "port" {
  description = "The SSL port for the Redis cache."
  value       = azurerm_redis_cache.this.ssl_port
}

output "private_endpoint_id" {
  description = "The ID of the Redis private endpoint, if enabled."
  value       = try(azurerm_private_endpoint.this[0].id, null)
}

output "private_endpoint_private_ip" {
  description = "The private IP of the Redis private endpoint, if enabled."
  value       = try(azurerm_private_endpoint.this[0].private_service_connection[0].private_ip_address, null)
}
