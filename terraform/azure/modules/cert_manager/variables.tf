variable "aks_dependency" {
  description = "Dependency on the AKS module to ensure cert-manager is installed after AKS."
  type        = any
}

variable "ingress_controller_dependency" {
  description = "Dependency on the ingress controller module to ensure cert-manager is installed after ingress."
  type        = any
}

variable "letsencrypt_email" {
  description = "Email address for Let's Encrypt ACME registration."
  type        = string
}

variable "dns_record_dependency" {
  description = "Dependency on the DNS record resource to ensure DNS is ready before issuing certificates."
  type        = any
}

variable "devapi_dns_name" {
  description = "DNS name for the devapi certificate."
  type        = string
}
