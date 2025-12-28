# -----------------------------------------------------------------
# K8s Addons Variables (Migrated from k8s/variables.tf)
# -----------------------------------------------------------------

variable "azure_dns_managed_identity_client_id" {
  type        = string
  description = "Client ID of the managed identity used by cert-manager to solve Azure DNS challenges"
  default     = "" # Will be populated from AKS module output if not provided
}

variable "api_subdomain" {
  type        = string
  description = "API subdomain (e.g. api, devapi)"
  default     = "api"
}

variable "app_namespace" {
  type    = string
  default = "default"
}

# KEDA Configuration
variable "keda_namespace" {
  type        = string
  description = "Namespace for KEDA system components"
  default     = "keda-system"
}
