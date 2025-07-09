variable "cert_manager_install_dependency" {
  description = "Dependency on the cert-manager Helm release to ensure CRDs are installed before creating issuers/certificates."
  type        = any
}

variable "letsencrypt_email" {
  description = "Email address for Let's Encrypt ACME registration."
  type        = string
}

variable "dns_record_dependency" {
  description = "Dependency on DNS record to ensure DNS is ready before issuing certificates."
  type        = any
}

variable "namespace" {
  description = "The namespace where the certificate will be created"
  type        = string
}

variable "domain_name" {
  description = "The domain name for the certificate"
  type        = string
}
