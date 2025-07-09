# -----------------------------------------------------------------
# Cert-Manager: Kubernetes certificate management via Helm (Stage 1)
# -----------------------------------------------------------------
module "cert_manager" {
  source                        = "../../modules/cert_manager"
  aks_dependency                = var.aks_dependency
  ingress_controller_dependency = var.ingress_controller_dependency
  namespace                     = var.namespace
}

# -----------------------------------------------------------------
# Cert-Manager Issuers and Certificates (Stage 2)
# -----------------------------------------------------------------
module "cert_manager_issuers" {
  source                          = "../../modules/cert_manager_issuers"
  cert_manager_install_dependency = module.cert_manager
  letsencrypt_email               = "nikhil@copilotgtm.com"
  dns_record_dependency           = var.dns_record_dependency
  namespace                       = var.namespace
  domain_name                     = "devapi.copilotgtm.com"
}
