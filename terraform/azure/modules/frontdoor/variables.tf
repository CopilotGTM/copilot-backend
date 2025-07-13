variable "prefix" {
  description = "The prefix for all resources"
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group"
  type        = string
}

variable "tags" {
  description = "The tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "frontend_endpoint_name" {
  description = "The name of the frontend endpoint"
  type        = string
}

variable "backend_address" {
  description = "The address of the backend"
  type        = string
}

variable "backend_host_header" {
  description = "The host header of the backend"
  type        = string
}