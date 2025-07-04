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