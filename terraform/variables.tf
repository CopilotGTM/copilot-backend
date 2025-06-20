variable "env" {
  description = "Environment identifier (e.g., dev, prod, staging)."
  type        = string
  default     = "dev"
}

variable "resource_group_name" {
  description = "The base name of the Azure Resource Group to create (the environment will be suffixed)."
  type        = string
}

variable "subscription_id" {
  description = "The Azure subscription ID to use. Required for the azurerm provider."
  type        = string
}

variable "location" {
  description = "The Azure region for the Resource Group."
  type        = string
  default     = "East US"
}

variable "vnet_address_space" {
  description = "Address space list for the Virtual Network."
  type        = list(string)
  default     = ["10.0.0.0/16"]
}

variable "vnet_public_subnets" {
  description = "Address prefixes for public subnets (list of CIDRs)."
  type        = list(string)
  default     = ["10.0.1.0/24"]
}

variable "vnet_private_subnets" {
  description = "Address prefixes for private subnets (list of CIDRs)."
  type        = list(string)
  default     = ["10.0.2.0/24"]
}