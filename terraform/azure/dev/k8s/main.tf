# -----------------------------------------------------------------
# 1) Helm chart: nginx-ingress
# -----------------------------------------------------------------
resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = var.ingress_namespace
  version          = "4.9.0"
  create_namespace = true

  values = [
    <<EOF
controller:
  replicaCount: 2
  ingressClassResource:
    name: nginx
    enabled: true
EOF
  ]
}

# -----------------------------------------------------------------
# 7) Create a certificate for the devapi
# -----------------------------------------------------------------
resource "kubernetes_manifest" "devapi_certificate" {
  manifest = {
    "apiVersion" = "cert-manager.io/v1"
    "kind"       = "Certificate"
    "metadata" = {
      "name"      = "devapi-tls"
      "namespace" = var.app_namespace
    }
    "spec" = {
      "secretName" = "devapi-tls"
      "issuerRef" = {
        "name" = "letsencrypt"
        "kind" = "ClusterIssuer"
      }
      "commonName" = "devapi.${var.dns_zone_name}"
      "dnsNames" = [
        "devapi.${var.dns_zone_name}"
      ]
    }
  }

  depends_on = [
    kubernetes_manifest.letsencrypt_issuer,
    kubernetes_namespace.app_namespace
  ]
}


# -----------------------------------------------------------------
# 2) Helm chart: cert-manager
# -----------------------------------------------------------------
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  version          = "1.18.2"
  create_namespace = true

  set = [
    {
      name  = "installCRDs"
      value = "true"
    }
  ]
}

# -----------------------------------------------------------------
# 3) cert-manager ClusterIssuer (Let’s Encrypt)
# -----------------------------------------------------------------
resource "time_sleep" "wait_for_cert_manager_crd" {
  create_duration = "30s"

  triggers = {
    cert_manager_release = helm_release.cert_manager.status
  }
}

resource "kubernetes_manifest" "letsencrypt_issuer" {
  manifest = {
    "apiVersion" = "cert-manager.io/v1"
    "kind"       = "ClusterIssuer"
    "metadata" = {
      "name" = "letsencrypt"
    }
    "spec" = {
      "acme" = {
        "email"  = "nikhil@copilotgtm.com"
        "server" = "https://acme-v02.api.letsencrypt.org/directory"
        "privateKeySecretRef" = {
          "name" = "letsencrypt-account-key"
        }
        "solvers" = [
          {
            "http01" = {
              "ingress" = {
                "class" = "nginx"
              }
            }
          },
          {
            "dns01" = {
              "azureDNS" = {
                "managedIdentity" = {
                  "clientID" = "d999da04-d31f-43f5-b27d-2a6ddce80e13"
                }
                "subscriptionID" = "675df0ed-8e06-441b-a1e4-110d19521816"
                "resourceGroupName" = "master-resources"
                "hostedZoneName" = "copilotgtm.com"
                "environment" = "AzurePublicCloud"
              }
            }
          }
        ]
      }
    }
  }

  depends_on = [
    # This dependency should point to a resource that ensures the
    # cert-manager CRDs are ready. For example, a time_sleep
    # or another resource that indicates cert-manager is fully installed.
    time_sleep.wait_for_cert_manager_crd
  ]
}

# -----------------------------------------------------------------
# 4) Wait for the nginx-ingress Service to exist & grab its IP
# -----------------------------------------------------------------
data "kubernetes_service" "nginx_ingress" {
  # force Terraform to create the Helm release first
  depends_on = [helm_release.ingress_nginx]

  metadata {
    name      = var.ingress_service_name
    namespace = var.ingress_namespace
  }
}

# -----------------------------------------------------------------
# 5) Create the application namespace
# -----------------------------------------------------------------
resource "kubernetes_namespace" "app_namespace" {
  metadata {
    name = var.app_namespace
  }
}

# -----------------------------------------------------------------
# 6) Create the DNS A record pointing at that IP
# -----------------------------------------------------------------
resource "azurerm_dns_a_record" "devapi" {
  name                = "devapi"
  zone_name           = var.dns_zone_name
  resource_group_name = var.dns_zone_resource_group
  ttl                 = 300

  records = [
    data.kubernetes_service.nginx_ingress
      .status[0]
      .load_balancer[0]
      .ingress[0]
      .ip
  ]
}
