variable "vnet_name" {
  description = "Name of the Virtual Network."
  type        = string
}

variable "address_space" {
  description = "The address space that is used by the virtual network."
  type        = list(string)
}

variable "location" {
  description = "Location for the VNet (should match the resource group location)."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group in which to create the VNet."
  type        = string
}

variable "public_subnets" {
  description = "List of address prefixes for public subnets."
  type        = list(string)
}

variable "private_subnets" {
  description = "List of address prefixes for private subnets."
  type        = list(string)
}

variable "env" {
  description = "Environment identifier (e.g., dev, prod, staging)."
  type        = string
}

variable "private_subnet_dependencies" {
  description = "A list of external resources that the private subnets depend on. This is used to enforce proper destruction order."
  type        = any
  default     = []
}