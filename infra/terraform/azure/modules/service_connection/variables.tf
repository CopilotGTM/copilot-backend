variable "devops_project_id" {
  description = "The Azure DevOps project ID."
  type        = string
}

variable "service_connection_name" {
  description = "The name for the Azure DevOps service connection."
  type        = string
}

variable "subscription_id" {
  description = "The Azure subscription ID."
  type        = string
}

variable "subscription_name" {
  description = "The Azure subscription name."
  type        = string
}

variable "tenant_id" {
  description = "The Azure AD tenant ID."
  type        = string
}

variable "client_id" {
  description = "Service principal client (application) ID."
  type        = string
}

# -----------------------------------------------------------------------------
# Authentication mode: OIDC (default) or Secret-based
# -----------------------------------------------------------------------------
variable "use_workload_identity_federation" {
  description = "Use Workload Identity Federation (OIDC) instead of client secret. Recommended for production."
  type        = bool
  default     = true
}

variable "client_secret" {
  description = "Service principal client secret. Required only when use_workload_identity_federation = false."
  type        = string
  sensitive   = true
  default     = ""
}
