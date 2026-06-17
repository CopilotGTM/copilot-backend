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

variable "private_endpoints_subnet_prefix" {
  description = "Optional CIDR prefix for a dedicated private endpoints subnet. If empty, no dedicated subnet is created."
  type        = string
  default     = ""
}

variable "managed_devops_pools_subnets" {
  description = "Optional dedicated subnets delegated to Microsoft.DevOpsInfrastructure/pools. Use one subnet per Managed DevOps Pool."
  type = list(object({
    name           = string
    address_prefix = string
  }))
  default = []
}

variable "enable_nat_gateway" {
  description = "Whether to create and attach a NAT Gateway for deterministic outbound internet egress."
  type        = bool
  default     = true
}
