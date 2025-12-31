variable "sp_name" {
  description = "The display name for the service principal."
  type        = string
}

variable "subscription_id" {
  description = "The subscription ID where the service principal will be granted Contributor access."
  type        = string
}

# -----------------------------------------------------------------------------
# OIDC / Workload Identity Federation for Azure DevOps
# -----------------------------------------------------------------------------
variable "enable_oidc" {
  description = "Enable Workload Identity Federation (OIDC) for Azure DevOps. When true, creates federated credential instead of client secret."
  type        = bool
  default     = true
}

variable "devops_org_name" {
  description = "Azure DevOps organization name (e.g., 'copilotgtm' from https://dev.azure.com/copilotgtm)."
  type        = string
  default     = ""
}

variable "devops_project_name" {
  description = "Azure DevOps project name for the federated credential subject."
  type        = string
  default     = ""
}

variable "service_connection_name" {
  description = "Azure DevOps service connection name for the federated credential subject."
  type        = string
  default     = ""
}

variable "create_password" {
  description = "Create a client secret for the service principal. Set to false when using OIDC."
  type        = bool
  default     = false
}

variable "enable_user_access_admin" {
  description = "Grant User Access Administrator role (needed if Terraform assigns RBAC roles)."
  type        = bool
  default     = true
}
