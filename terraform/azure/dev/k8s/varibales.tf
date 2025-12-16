variable "subscription_id" {
  type        = string
  description = "Azure subscription ID"
}

# DNS zone info
variable "dns_zone_name" {
  type        = string
  description = "DNS zone (e.g. copilotgtm.com)"
  default     = "copilotgtm.com"
}

variable "dns_zone_resource_group" {
  type        = string
  description = "Resource group where the DNS zone lives"
  default     = "master-resources"
}

variable "letsencrypt_email" {
  type        = string
  description = "Email address for Let's Encrypt registration"
}

variable "azure_dns_managed_identity_client_id" {
  type        = string
  description = "Client ID of the managed identity used by cert-manager to solve Azure DNS challenges"
}

variable "api_subdomain" {
  type        = string
  description = "API subdomain (e.g. api, devapi)"
  default     = "api"
}

# ingress-controller lookup
variable "ingress_namespace" {
  type        = string
  description = "Namespace of the nginx-ingress controller"
  default     = "ingress-nginx"
}

variable "ingress_service_name" {
  type        = string
  description = "Name of the nginx-ingress controller Service"
  # chart <release>-controller → ingress-nginx-controller
  default     = "ingress-nginx-controller"
}

variable "app_namespace" {
  type    = string
  default = "default"
}

variable "app_service_name" {
  type    = string
  default = "your-backend-svc"
}

variable "app_service_port" {
  type    = number
  default = 80
}

# KEDA Configuration
variable "enable_keda_autoscaling" {
  type        = bool
  description = "Enable KEDA-based autoscaling for applications"
  default     = false
}

variable "keda_namespace" {
  type        = string
  description = "Namespace for KEDA system components"
  default     = "keda-system"
}
