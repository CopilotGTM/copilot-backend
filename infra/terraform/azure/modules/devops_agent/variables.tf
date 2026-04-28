# -----------------------------------------------------------------------------
# DevOps Self-Hosted Agent VM Module - Variables
# -----------------------------------------------------------------------------

variable "resource_group_name" {
  description = "Name of the resource group to deploy the agent VM into."
  type        = string
}

variable "location" {
  description = "Azure region for the agent VM."
  type        = string
}

variable "vm_name" {
  description = "Name of the virtual machine."
  type        = string
}

variable "vm_size" {
  description = "Size of the virtual machine."
  type        = string
  default     = "Standard_B2s"
}

variable "admin_username" {
  description = "Admin username for the VM."
  type        = string
  default     = "azureuser"
}

variable "admin_password" {
  description = "Admin password for the VM. Required if ssh_public_key is not provided."
  type        = string
  sensitive   = true
  default     = ""
}

variable "ssh_public_key" {
  description = "SSH public key for the VM. If provided, password auth is disabled."
  type        = string
  default     = ""
}

variable "subnet_id" {
  description = "ID of the subnet to place the agent VM in. Should be a private subnet with access to Private Link resources."
  type        = string
}

variable "azuredevops_url" {
  description = "Azure DevOps organization URL (e.g., https://dev.azure.com/myorg)."
  type        = string
}

variable "azuredevops_pat" {
  description = "Personal Access Token for registering the agent with Azure DevOps."
  type        = string
  sensitive   = true
}

variable "agent_pool" {
  description = "Name of the Azure DevOps agent pool to register this agent in."
  type        = string
}

variable "agent_name" {
  description = "Name to assign to this DevOps agent instance."
  type        = string
}

variable "os_disk_size_gb" {
  description = "Size of the OS disk in GB."
  type        = number
  default     = 128
}

variable "tags" {
  description = "Tags to apply to all resources."
  type        = map(string)
  default     = {}
}

variable "enable_public_ip" {
  description = "Whether to assign a public IP to the VM. Set to false for fully private access."
  type        = bool
  default     = false
}

variable "install_docker" {
  description = "Whether to install Docker on the agent VM."
  type        = bool
  default     = true
}

variable "install_terraform" {
  description = "Whether to install Terraform on the agent VM."
  type        = bool
  default     = true
}

variable "terraform_version" {
  description = "Version of Terraform to install."
  type        = string
  default     = "1.9.0"
}

variable "install_kubectl" {
  description = "Whether to install kubectl on the agent VM."
  type        = bool
  default     = true
}

variable "install_azure_cli" {
  description = "Whether to install Azure CLI on the agent VM."
  type        = bool
  default     = true
}
