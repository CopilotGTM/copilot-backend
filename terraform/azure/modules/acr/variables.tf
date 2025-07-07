variable "acr_name" {
  description = "The name of the Azure Container Registry."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group in which to create the ACR."
  type        = string
}

variable "location" {
  description = "The Azure location/region where ACR will be deployed."
  type        = string
}

variable "sku" {
  description = "The SKU of the Azure Container Registry (e.g., Basic, Standard, Premium)."
  type        = string
  default     = "Basic"
}

variable "subnet_id" {
  description = "The ID of the subnet for the private endpoint"
  type        = string
}

variable "vnet_id" {
  description = "The ID of the virtual network for DNS linking"
  type        = string
}
