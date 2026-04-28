variable "name" {
  description = "The name of the user-assigned managed identity."
  type        = string
}

variable "location" {
  description = "The Azure region where the user-assigned managed identity should be created."
  type        = string
}

variable "resource_group_name" {
  description = "The resource group in which to create the user-assigned managed identity."
  type        = string
}
