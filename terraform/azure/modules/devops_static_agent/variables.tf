variable "name_prefix" {
  description = "Prefix for VM and resource names"
  type        = string
}

variable "location" {
  description = "Azure location"
  type        = string
}

variable "resource_group_name" {
  description = "Azure resource group"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for VM NIC"
  type        = string
}

variable "admin_username" {
  description = "Admin username"
  type        = string
  default     = "azureuser"
}

variable "admin_password" {
  description = "Admin password"
  type        = string
  sensitive   = true
}

variable "org_service_url" {
  description = "Azure DevOps organization URL (e.g., https://dev.azure.com/yourorg)."
  type        = string
}

variable "agent_pool" {
  description = "Name of the Azure DevOps agent pool to register this agent in."
  type        = string
}

variable "azuredevops_pat" {
  description = "Azure DevOps Personal Access Token for registering self-hosted agent."
  type        = string
  sensitive   = true
}

variable "agent_name" {
  description = "Name to assign to this DevOps agent instance."
  type        = string
}