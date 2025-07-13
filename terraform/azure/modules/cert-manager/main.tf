# modules/cert-manager/main.tf

terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
    }
    helm = {
      source  = "hashicorp/helm"
    }
  }
}

# Create namespace for cert-manager
resource "kubernetes_namespace" "cert_manager" {
  metadata {
    name = var.namespace
    labels = {
      "cert-manager.io/disable-validation" = "true"
    }
  }
}

# Install cert-manager using Helm
resource "helm_release" "cert_manager" {
  name       = var.release_name
  repository = "https://charts.jetstack.io"
  chart      = "cert-manager"
  namespace  = var.namespace
  version    = var.chart_version
  
  timeout = 600
  wait    = true
  wait_for_jobs = true
  
  # Install CRDs
  set = [{
    name  = "installCRDs"
    value = "true"
  }
  
  # Enable webhook
  ,{
    name  = "webhook.enabled"
    value = "true"
  }
  
  # Enable cainjector
  ,{
    name  = "cainjector.enabled"
    value = "true"
  }
  
  # Set resource limits
  ,{
    name  = "resources.limits.cpu"
    value = "100m"
  }

  ,{
    name  = "resources.limits.memory"
    value = "128Mi"
  }
  
  ,{
    name  = "resources.requests.cpu"
    value = "10m"
  }
  
  ,{
    name  = "resources.requests.memory"
    value = "32Mi"
  }
  ]
  
  depends_on = [kubernetes_namespace.cert_manager]
}

# Create ClusterIssuer for Let's Encrypt Production
resource "kubernetes_manifest" "letsencrypt_prod_issuer" {
  depends_on = [helm_release.cert_manager]
  manifest = {
    "apiVersion" = "cert-manager.io/v1"
    "kind"       = "ClusterIssuer"
    "metadata" = {
      "name" = var.letsencrypt_prod_issuer_name
    }
    "spec" = {
      "acme" = {
        "server" = "https://acme-v02.api.letsencrypt.org/directory"
        "email"  = var.letsencrypt_email
        "privateKeySecretRef" = {
          "name" = var.letsencrypt_prod_issuer_name
        }
        "solvers" = [
          {
            "http01" = {
              "ingress" = {
                "class" = var.ingress_class
              }
            }
          }
        ]
      }
    }
  }
}

# Create ClusterIssuer for Let's Encrypt Staging
resource "kubernetes_manifest" "letsencrypt_staging_issuer" {
  count      = var.create_staging_issuer ? 1 : 0
  depends_on = [helm_release.cert_manager]
  manifest = {
    "apiVersion" = "cert-manager.io/v1"
    "kind"       = "ClusterIssuer"
    "metadata" = {
      "name" = var.letsencrypt_staging_issuer_name
    }
    "spec" = {
      "acme" = {
        "server" = "https://acme-staging-v02.api.letsencrypt.org/directory"
        "email"  = var.letsencrypt_email
        "privateKeySecretRef" = {
          "name" = var.letsencrypt_staging_issuer_name
        }
        "solvers" = [
          {
            "http01" = {
              "ingress" = {
                "class" = var.ingress_class
              }
            }
          }
        ]
      }
    }
  }
}

# Create certificates based on the certificates variable
resource "kubernetes_manifest" "certificates" {
  for_each   = var.certificates
  depends_on = [kubernetes_manifest.letsencrypt_prod_issuer]

  manifest = {
    "apiVersion" = "cert-manager.io/v1"
    "kind"       = "Certificate"
    "metadata" = {
      "name"      = each.value.name
      "namespace" = each.value.namespace
    }
    "spec" = {
      "secretName" = each.value.secret_name
      "issuerRef" = {
        "name" = each.value.issuer_name
        "kind" = each.value.issuer_kind
      }
      "commonName" = each.value.common_name
      "dnsNames"   = each.value.dns_names
    }
  }
}

# Create a test certificate if enabled
resource "kubernetes_manifest" "test_certificate" {
  count      = var.create_test_certificate ? 1 : 0
  depends_on = [kubernetes_manifest.letsencrypt_staging_issuer]

  manifest = {
    "apiVersion" = "cert-manager.io/v1"
    "kind"       = "Certificate"
    "metadata" = {
      "name"      = "test-certificate"
      "namespace" = var.test_certificate_namespace
    }
    "spec" = {
      "secretName" = "test-certificate-tls"
      "issuerRef" = {
        "name" = var.letsencrypt_staging_issuer_name
        "kind" = "ClusterIssuer"
      }
      "commonName" = var.test_certificate_domain
      "dnsNames" = [
        var.test_certificate_domain
      ]
    }
  }
}