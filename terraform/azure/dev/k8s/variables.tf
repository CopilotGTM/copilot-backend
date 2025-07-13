variable "resource_group_name" {
  type        = string
  description = "Resource group name for Azure resources"
}

variable "location" {
  type        = string
  description = "Azure region for resource deployment"
}

variable "subscription_id" {
  type        = string
  description = "Azure subscription ID"
}