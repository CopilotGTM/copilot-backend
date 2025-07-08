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

variable "devapi_dns_name" {
  description = "DNS name for the devapi certificate."
  type        = string
}
