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

variable "admin_enabled" {
  description = "Whether admin user is enabled for ACR."
  type        = bool
  default     = false
}