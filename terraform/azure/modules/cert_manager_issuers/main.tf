# Cert-Manager Issuers and Certificates: To be applied after cert-manager is installed and ready

resource "null_resource" "wait_for_clusterissuer_crd" {
  provisioner "local-exec" {
    command = <<EOT
      for i in {1..30}; do
        kubectl get crd clusterissuers.cert-manager.io && \
        kubectl -n cert-manager get deploy cert-manager-webhook && \
        [ "$(kubectl -n cert-manager get deploy cert-manager-webhook -o jsonpath='{.status.availableReplicas}')" -ge 1 ] && exit 0
        sleep 10
      done
      echo "Timeout waiting for cert-manager CRDs and webhook"
      exit 1
    EOT
    interpreter = ["/bin/bash", "-c"]
  }
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
    null_resource.wait_for_clusterissuer_crd,
    var.dns_record_dependency
  ]
}

resource "kubernetes_manifest" "devapi_cert" {
  manifest = {
    apiVersion = "cert-manager.io/v1"
    kind       = "Certificate"
    metadata = {
      name      = "devapi-copilotgtm-com"
      namespace = "ingress-nginx"
    }
    spec = {
      secretName = "devapi-copilotgtm-com-tls"
      issuerRef = {
        name = "letsencrypt-prod"
        kind = "ClusterIssuer"
      }
      dnsNames = [var.devapi_dns_name]
    }
  }
  depends_on = [
    kubernetes_manifest.letsencrypt_clusterissuer,
    var.dns_record_dependency
  ]
}
