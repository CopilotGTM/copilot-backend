resource "azurerm_public_ip" "aks_static_ip" {
  name                = "${var.release_name}-static-ip"
  location            = var.location
  resource_group_name = var.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}

data "kubernetes_namespace" "ingress_namespace" {
  metadata {
    name = var.namespace
  }
}

resource "helm_release" "nginx_ingress" {
  depends_on = [azurerm_public_ip.aks_static_ip, data.kubernetes_namespace.ingress_namespace]
  name       = var.release_name
  repository = "https://kubernetes.github.io/ingress-nginx"
  chart      = "ingress-nginx"
  namespace  = var.namespace
  version    = var.chart_version

  set = [
    {
      name  = "controller.service.loadBalancerIP"
      value = azurerm_public_ip.aks_static_ip.ip_address
    },
    {
      name  = "controller.service.externalTrafficPolicy"
      value = "Local"
    },
    {
      name  = "controller.replicaCount"
      value = var.replica_count
    }
  ]
}

# Fetch the nginx ingress controller LoadBalancer service to get its public IP
data "kubernetes_service" "nginx_ingress" {
  metadata {
    name      = "${var.release_name}-ingress-nginx-controller"
    namespace = var.namespace
  }
  depends_on = [helm_release.nginx_ingress]
}
