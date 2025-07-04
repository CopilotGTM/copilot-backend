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