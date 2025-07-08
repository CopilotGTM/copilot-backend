resource "helm_release" "nginx_ingress" {
  name       = var.release_name
  repository = "https://kubernetes.github.io/ingress-nginx"
  chart      = "ingress-nginx"
  version    = var.chart_version
  namespace  = var.namespace

  create_namespace = true

  set = [
    {
      name  = "controller.service.type"
      value = var.service_type
    },
    {
      name  = "controller.replicaCount"
      value = var.replica_count
    },
    {
      name  = "controller.service.ports.http"
      value = "80"
    }
  ]

  # Ensure AKS cluster is ready before installing ingress
  depends_on = [var.aks_dependency]
}

# Fetch the nginx ingress controller LoadBalancer service to get its public IP
data "kubernetes_service" "nginx_ingress" {
  metadata {
    name      = "nginx-ingress-ingress-nginx-controller"
    namespace = var.namespace
  }
  depends_on = [helm_release.nginx_ingress]
}
