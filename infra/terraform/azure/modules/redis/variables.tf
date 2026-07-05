variable "redis_name" {
  description = "Name of the Redis cache instance."
  type        = string
}

variable "location" {
  description = "Azure region for Redis."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group in which to create the Redis cache."
  type        = string
}

variable "capacity" {
  description = "The size/capacity of the Redis cache (0: 250MB, 1: 1GB, 2: 2.5GB)."
  type        = number
  default     = 0
}

variable "family" {
  description = "SKU family (C = Basic/Standard, P = Premium)."
  type        = string
  default     = "C"
}


variable "tags" {
  description = "Tags to apply to resources."
  type        = map(string)
  default     = {}
}

variable "sku_name" {
  description = "The SKU of the Redis cache."
  type        = string
  default     = "Standard"
}

variable "public_network_access_enabled" {
  description = "Whether public network access is enabled for Redis."
  type        = bool
  default     = true
}

variable "private_endpoint_enabled" {
  description = "Whether to create a Private Endpoint for Redis."
  type        = bool
  default     = false
}

variable "private_endpoint_subnet_id" {
  description = "Subnet ID where the Redis Private Endpoint should be created."
  type        = string
  default     = null
}

variable "private_dns_zone_id" {
  description = "Private DNS Zone ID for Redis Private Link."
  type        = string
  default     = null
}
