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

  set = [
    {
      name  = "controller.replicaCount"
      value = "2"
    },
    {
      name  = "controller.ingressClassResource.name"
      value = "nginx"
    },
    {
      name  = "controller.ingressClassResource.enabled"
      value = "true"
    },
    {
      name  = "controller.service.type"
      value = "LoadBalancer"
    },
    {
      name  = "controller.service.externalTrafficPolicy"
      value = "Local"
    },
    {
      name  = "controller.service.annotations.service\\.beta\\.kubernetes\\.io/azure-load-balancer-internal"
      value = "false"
    }
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
# 2.1) Helm chart: KEDA (Kubernetes Event-driven Autoscaling)
# -----------------------------------------------------------------
resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  namespace        = "keda-system"
  version          = "2.15.1"
  create_namespace = true

  set = [
    {
      name  = "resources.operator.requests.cpu"
      value = "100m"
    },
    {
      name  = "resources.operator.requests.memory"
      value = "128Mi"
    },
    {
      name  = "resources.operator.limits.cpu"
      value = "1000m"
    },
    {
      name  = "resources.operator.limits.memory"
      value = "1000Mi"
    },
    {
      name  = "resources.metricServer.requests.cpu"
      value = "100m"
    },
    {
      name  = "resources.metricServer.requests.memory"
      value = "128Mi"
    },
    {
      name  = "resources.metricServer.limits.cpu"
      value = "1000m"
    },
    {
      name  = "resources.metricServer.limits.memory"
      value = "1000Mi"
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
        "email"  = var.letsencrypt_email
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
                  "clientID" = var.azure_dns_managed_identity_client_id
                }
                "subscriptionID"    = var.subscription_id
                "resourceGroupName" = var.dns_zone_resource_group
                "hostedZoneName"    = var.dns_zone_name
                "environment"       = "AzurePublicCloud"
              }
            }
          }
        ]
      }
    }
  }

  depends_on = [
    time_sleep.wait_for_cert_manager_crd
  ]
}

# -----------------------------------------------------------------
# 4) Wait for the nginx-ingress Service to exist & grab its IP
# -----------------------------------------------------------------
data "kubernetes_service" "nginx_ingress" {
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
resource "azurerm_dns_a_record" "api" {
  name                = var.api_subdomain
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

# -----------------------------------------------------------------
# 7) Create a certificate for the API
# -----------------------------------------------------------------
resource "kubernetes_manifest" "api_certificate" {
  manifest = {
    "apiVersion" = "cert-manager.io/v1"
    "kind"       = "Certificate"
    "metadata" = {
      "name"      = "${var.api_subdomain}-tls"
      "namespace" = var.app_namespace
    }
    "spec" = {
      "secretName" = "${var.api_subdomain}-tls"
      "issuerRef" = {
        "name" = "letsencrypt"
        "kind" = "ClusterIssuer"
      }
      "commonName" = "${var.api_subdomain}.${var.dns_zone_name}"
      "dnsNames"   = ["${var.api_subdomain}.${var.dns_zone_name}"]
    }
  }

  depends_on = [
    kubernetes_manifest.letsencrypt_issuer,
    kubernetes_namespace.app_namespace
  ]
}
