# -----------------------------------------------------------------
# Cert-Manager: Kubernetes certificate management via Helm (Stage 1)
# -----------------------------------------------------------------
module "cert_manager" {
  source                        = "../../modules/cert_manager"
  aks_dependency                = module.aks
  ingress_controller_dependency = module.ingress_controller
  namespace                     = module.ingress_controller.nginx_ingress_namespace
}

# -----------------------------------------------------------------
# Cert-Manager Issuers and Certificates (Stage 2)
# -----------------------------------------------------------------
module "cert_manager_issuers" {
  source                          = "../../modules/cert_manager_issuers"
  cert_manager_install_dependency = module.cert_manager
  letsencrypt_email               = "nikhil@copilotgtm.com"
  dns_record_dependency           = azurerm_dns_a_record.devapi
  namespace                       = module.ingress_controller.nginx_ingress_namespace
  domain_name                     = "devapi.copilotgtm.com"
}

# -----------------------------------------------------------------
# Ingress Controller (nginx) for AKS
# -----------------------------------------------------------------
