terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
    }
    helm = {
      source  = "hashicorp/helm"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
    }
  }
}
// NOTE: Providers (azurerm, kubernetes, helm) are expected to be configured and injected by the root module.
// Ensure you have your AKS kubeconfig populated (e.g., `az aks get-credentials ...`).

resource "azurerm_public_ip" "nginx_ip" {
  name                = "${var.release_name}-ip"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "Standard"
  allocation_method   = "Static"
}

resource "kubernetes_namespace" "ingress" {
  metadata {
    name = var.namespace
  }
}

resource "helm_release" "nginx" {
  name       = var.release_name
  repository = "https://kubernetes.github.io/ingress-nginx"
  chart      = "ingress-nginx"
  namespace  = var.namespace
  version    = var.chart_version

  set = [
    {
      name  = "controller.service.annotations.\"service\\.beta\\.kubernetes\\.io/azure-load-balancer-resource-group\""
      value = var.resource_group_name
    },
  {
    name  = "controller.service.loadBalancerIP"
    value = azurerm_public_ip.nginx_ip.ip_address
  }
  ]

  depends_on = [azurerm_public_ip.nginx_ip]
  timeout = 900
}