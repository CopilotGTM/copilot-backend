variable "name" {
  description = "Base name for the workload identity application/SP"
  type        = string
}

variable "oidc_issuer_url" {
  description = "AKS OIDC issuer URL"
  type        = string
}

variable "service_account_namespace" {
  description = "Kubernetes namespace of the service account"
  type        = string
}

variable "service_account_name" {
  description = "Kubernetes service account name"
  type        = string
}

variable "audience" {
  description = "Federated credential audiences"
  type        = list(string)
  default     = ["api://AzureADTokenExchange"]
}

variable "create_password" {
  description = "Whether to create a client secret (not needed for workload identity)"
  type        = bool
  default     = false
}
