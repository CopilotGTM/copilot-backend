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
