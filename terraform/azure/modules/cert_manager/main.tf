# Cert-Manager: Kubernetes certificate management via Helm

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true
  version          = "v1.18.2"

  values = [
    yamlencode({
      installCRDs = true
    })
  ]

  wait    = true
  timeout = 600

  depends_on = [
    var.aks_dependency,
    var.ingress_controller_dependency
  ]
}
