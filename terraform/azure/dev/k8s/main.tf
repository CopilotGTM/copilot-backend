# Kubernetes Namespace for Copilot Apps
resource "kubernetes_namespace" "copilotapps" {
  metadata {
    name = "copilot"
  }
}

# Cert-Manager Helm Release and Certificate Management
module "cert_manager" {
  source = "../modules/cert-manager"

  namespace         = "cert-manager"
  release_name      = "cert-manager"
  chart_version     = "v1.13.3"
  letsencrypt_email = "nikhil@copilotgtm.com"

  # Create both production and staging issuers
  create_staging_issuer = true

  # Define your certificates
  certificates = {
    "api-copilotgtm-com" = {
      name        = "api-copilotgtm-com"
      namespace   = "copilot"
      secret_name = "api-copilotgtm-com-tls"
      issuer_name = "letsencrypt-prod"
      issuer_kind = "ClusterIssuer"
      common_name = "devapi.copilotgtm.com"
      dns_names   = ["devapi.copilotgtm.com"]
    },
    "web-copilotgtm-com" = {
      name        = "web-copilotgtm-com"
      namespace   = "copilot"
      secret_name = "web-copilotgtm-com-tls"
      issuer_name = "letsencrypt-prod"
      issuer_kind = "ClusterIssuer"
      common_name = "devweb.copilotgtm.com"
      dns_names   = ["devweb.copilotgtm.com", "www.devweb.copilotgtm.com"]
    }
  }

  # Optional: Create a test certificate for validation
  create_test_certificate     = false
  test_certificate_namespace  = "default"
  test_certificate_domain     = "test.copilotgtm.com"
}

# NGINX Ingress Controller Helm Release
module "ingress_controller" {
  source              = "../modules/ingress-controller"
  resource_group_name = var.resource_group_name
  location            = var.location
  namespace           = "nginx-ingress"
  release_name        = "ingress-nginx"
  values_file_path    = "${path.module}/../modules/ingress-controller/values.yaml"
  providers = {
    kubernetes = kubernetes
    helm       = helm
  }
  depends_on          = [module.cert_manager]
}

# Ensure cert-manager is installed after ingress controller
resource "null_resource" "module_dependency" {
  depends_on = [
    module.ingress_controller,
    module.cert_manager
  ]

  provisioner "local-exec" {
    command = "echo 'Both ingress controller and cert-manager are ready'"
  }
}

output "ingress_controller_public_ip" {
  description = "The public IP address of the NGINX ingress controller"
  value       = module.ingress_controller.nginx_ingress_public_ip
}

resource "azurerm_dns_zone" "copilotgtm" {
  name                = "copilotgtm.com"
  resource_group_name = "master-resources"
}

resource "azurerm_dns_a_record" "devapi" {
  name                = "devapi"
  zone_name           = azurerm_dns_zone.copilotgtm.name
  resource_group_name = azurerm_dns_zone.copilotgtm.resource_group_name
  ttl                 = 300
  records             = [module.ingress_controller.nginx_ingress_public_ip]
  depends_on          = [module.ingress_controller]
}
