variable "subscription_id" {
  description = "Azure subscription ID to deploy resources into"
  type        = string
}

variable "location" {
  description = "Azure region for all resources"
  type        = string
  default     = "West US"
}

variable "env" {
  description = "Environment identifier"
  type        = string
  default     = "prod"
}

variable "resource_group_name" {
  description = "Name of the resource group for Nango"
  type        = string
  default     = "copilot-nango-prod"
}

variable "nango_host" {
  description = "Public hostname for the Nango deployment"
  type        = string
  default     = "nango.copilotgtm.com"
}

variable "vnet_name" {
  description = "Name of the Nango virtual network"
  type        = string
  default     = "nango-vnet-prod"
}

variable "vnet_address_space" {
  description = "CIDR blocks for the Nango virtual network"
  type        = list(string)
  default     = ["10.60.0.0/16"]
}

variable "public_subnets" {
  description = "Public subnet CIDR ranges"
  type        = list(string)
  default     = ["10.60.1.0/24"]
}

variable "private_subnets" {
  description = "Private subnet CIDR ranges"
  type        = list(string)
  default     = ["10.60.2.0/24"]
}
