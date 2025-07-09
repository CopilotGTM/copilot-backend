# Cert-Manager: Kubernetes certificate management via Helm

resource "helm_release" "cert_manager" {
  depends_on = [var.aks_dependency, var.ingress_controller_dependency]
  name       = "cert-manager"
  repository = "https://charts.jetstack.io"
  chart      = "cert-manager"
  namespace  = var.namespace
  version    = "1.12.0"

  values = [
    yamlencode({
      installCRDs = true
    })
  ]
}
