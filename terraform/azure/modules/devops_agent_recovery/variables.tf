# -----------------------------------------------------------------------------
# DevOps Agent Spot VM Recovery Module - Variables
# -----------------------------------------------------------------------------

variable "resource_group_name" {
  description = "Name of the resource group containing the agent VM."
  type        = string
}

variable "location" {
  description = "Azure region for the automation account."
  type        = string
}

variable "automation_account_name" {
  description = "Name of the Azure Automation Account."
  type        = string
}

variable "vm_name" {
  description = "Name of the spot VM to monitor and recover."
  type        = string
}

variable "vm_id" {
  description = "Resource ID of the spot VM to monitor and recover."
  type        = string
}

variable "check_interval_minutes" {
  description = "How often (in minutes) to check if the VM is deallocated and restart it."
  type        = number
  default     = 5
}

variable "tags" {
  description = "Tags to apply to all resources."
  type        = map(string)
  default     = {}
}
