# modules/cert-manager/variables.tf

variable "namespace" {
  description = "Namespace for cert-manager installation"
  type        = string
  default     = "cert-manager"
}

variable "release_name" {
  description = "Name of the Helm release"
  type        = string
  default     = "cert-manager"
}

variable "chart_version" {
  description = "Version of cert-manager chart"
  type        = string
  default     = "v1.13.3"
}

variable "letsencrypt_email" {
  description = "Email address for Let's Encrypt registration"
  type        = string
}

variable "letsencrypt_prod_issuer_name" {
  description = "Name for the Let's Encrypt production issuer"
  type        = string
  default     = "letsencrypt-prod"
}

variable "letsencrypt_staging_issuer_name" {
  description = "Name for the Let's Encrypt staging issuer"
  type        = string
  default     = "letsencrypt-staging"
}

variable "ingress_class" {
  description = "Ingress class to use for HTTP01 challenges"
  type        = string
  default     = "nginx"
}

variable "create_staging_issuer" {
  description = "Whether to create a staging issuer for testing"
  type        = bool
  default     = true
}

variable "certificates" {
  description = "Map of certificates to create"
  type = map(object({
    name        = string
    namespace   = string
    secret_name = string
    issuer_name = string
    issuer_kind = string
    common_name = string
    dns_names   = list(string)
  }))
  default = {}
}

variable "create_test_certificate" {
  description = "Whether to create a test certificate"
  type        = bool
  default     = false
}

variable "test_certificate_namespace" {
  description = "Namespace for test certificate"
  type        = string
  default     = "default"
}

variable "test_certificate_domain" {
  description = "Domain for test certificate"
  type        = string
  default     = "test.example.com"
}