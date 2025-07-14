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
# 2) Helm chart: cert-manager
# -----------------------------------------------------------------
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  version          = "1.13.3"
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
  manifest = yamldecode(<<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt
spec:
  acme:
    email: nikhil@copilotgtm.com
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-account-key
    solvers:
    - http01:
        ingress:
          class: nginx
EOF
  )

  depends_on = [
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

resource "kubernetes_ingress_v1" "technical_deck" {
  metadata {
    name      = "technical-deck"
    namespace = var.app_namespace
    annotations = {
      "cert-manager.io/cluster-issuer" = "letsencrypt"
    }
  }
  
  spec {
    ingress_class_name = "nginx"
    tls {
      hosts       = ["devapi.copilotgtm.com"]
      secret_name = "devapi-tls"
    }
    
    rule {
      host = "devapi.copilotgtm.com"
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = "technical-deck"  # Match actual service name
              port {
                number = 80
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    helm_release.ingress_nginx,
    data.kubernetes_service.nginx_ingress,
    kubernetes_namespace.app_namespace,
  ]
}