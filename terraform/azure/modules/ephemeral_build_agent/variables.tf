# --- VMSS Azure Resources ---
variable "vmss_name" {
  description = "Name for the Virtual Machine Scale Set."
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group to deploy the VMSS into."
  type        = string
}

variable "location" {
  description = "Azure region where the resources will be created."
  type        = string
}

variable "subnet_id" {
  description = "The ID of the subnet to deploy the VMSS instances into."
  type        = string
}

variable "vm_sku" {
  description = "The size of the virtual machines in the scale set (e.g., 'Standard_DS2_v2')."
  type        = string
  default     = "Standard_D2s_v3"
}

variable "admin_username" {
  description = "Admin username for the agent VMs."
  type        = string
  default     = "azureuser"
}

variable "admin_password" {
  description = "Admin password for the agent VMs."
  type        = string
  sensitive   = true
}

variable "min_instances" {
  description = "The minimum number of agents to keep warm."
  type        = number
  default     = 1
}

variable "project_tag" {
  description = "The project tag to apply to resources."
  type        = string
}


# --- Azure DevOps Configuration ---
variable "agent_pool_name" {
  description = "Name for the Azure DevOps agent pool."
  type        = string
}

variable "devops_project_name" {
  description = "Azure DevOps project name."
  type        = string
}

variable "org_service_url" {
  description = "Azure DevOps organization URL (e.g., https://dev.azure.com/yourorg)."
  type        = string
}

variable "azuredevops_pat" {
  description = "Azure DevOps Personal Access Token for registering the agent."
  type        = string
  sensitive   = true
}

variable "agent_name_prefix" {
  description = "Prefix for the agent names created in the pool."
  type        = string
}