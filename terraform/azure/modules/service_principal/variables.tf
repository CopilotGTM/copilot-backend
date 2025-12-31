variable "sp_name" {
  description = "The display name for the service principal."
  type        = string
}

variable "subscription_id" {
  description = "The subscription ID where the service principal will be granted Contributor access."
  type        = string
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
