# Cert-Manager Issuers and Certificates: To be applied after cert-manager is installed and ready

resource "time_sleep" "wait_for_crd_registration" {
  create_duration = "90s"

  depends_on = [var.cert_manager_install_dependency]
}

resource "kubernetes_manifest" "letsencrypt_clusterissuer" {
  manifest = {
    apiVersion = "cert-manager.io/v1"
    kind       = "ClusterIssuer"
    metadata = {
      name = "letsencrypt-prod"
    }
    spec = {
      acme = {
        email  = var.letsencrypt_email
        server = "https://acme-v02.api.letsencrypt.org/directory"
        privateKeySecretRef = {
          name = "letsencrypt-prod"
        }
        solvers = [{
          http01 = {
            ingress = {
              class = "nginx"
            }
          }
        }]
      }
    }
  }
  depends_on = [
    time_sleep.wait_for_crd_registration,
    var.dns_record_dependency
  ]
}

resource "kubernetes_manifest" "devapi_cert" {
  manifest = {
    apiVersion = "cert-manager.io/v1"
    kind       = "Certificate"
    metadata = {
      name      = "devapi-copilotgtm-com"
      namespace = var.namespace
    }
    spec = {
      secretName = "devapi-copilotgtm-com-tls"
      issuerRef = {
        name = "letsencrypt-prod"
        kind = "ClusterIssuer"
      }
      dnsNames = [var.domain_name]
    }
  }
  depends_on = [
    kubernetes_manifest.letsencrypt_clusterissuer,
    var.dns_record_dependency
  ]
}
