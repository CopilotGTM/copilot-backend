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
    null = {
      source = "hashicorp/null"
    }
  }
}

# NOTE: Providers (azurerm, kubernetes, helm) are expected to be configured and injected by the root module.
# Ensure you have your AKS kubeconfig populated (e.g., `az aks get-credentials ...`).

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
  
  # Add lifecycle rule to prevent recreation issues
  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
      metadata[0].labels
    ]
  }
}

resource "helm_release" "nginx" {
  name       = var.release_name
  repository = "https://kubernetes.github.io/ingress-nginx"
  chart      = "ingress-nginx"
  namespace  = var.namespace
  version    = var.chart_version
  
  # Reduced timeout since we don't wait for load balancer
  timeout = 600  # 10 minutes
  wait    = false  # Don't wait for load balancer
  wait_for_jobs = false
  
  # Disable atomic for better error handling
  atomic = false
  
  set = [
    {
      name  = "controller.service.annotations.\"service\\.beta\\.kubernetes\\.io/azure-load-balancer-resource-group\""
      value = var.resource_group_name
    },
    {
      name  = "controller.service.loadBalancerIP"
      value = azurerm_public_ip.nginx_ip.ip_address
    },
    # Azure-specific annotations
    {
      name  = "controller.service.annotations.\"service\\.beta\\.kubernetes\\.io/azure-load-balancer-internal\""
      value = "false"
    },
    {
      name  = "controller.service.annotations.\"service\\.beta\\.kubernetes\\.io/azure-pip-name\""
      value = azurerm_public_ip.nginx_ip.name
    },
    # Add additional stability settings
    {
      name  = "controller.service.externalTrafficPolicy"
      value = "Local"
    },
    {
      name  = "controller.replicaCount"
      value = "2"
    }
  ]
  
  depends_on = [
    azurerm_public_ip.nginx_ip,
    kubernetes_namespace.ingress
  ]
}

# Separate resource to wait for the load balancer IP assignment
resource "null_resource" "wait_for_load_balancer" {
  depends_on = [helm_release.nginx]
  
  provisioner "local-exec" {
    command = <<-EOT
      echo "Waiting for load balancer IP assignment..."
      timeout=1800  # 30 minutes
      interval=30   # Check every 30 seconds
      elapsed=0
      
      while [ $elapsed -lt $timeout ]; do
        external_ip=$(kubectl get svc ${var.release_name}-controller -n ${var.namespace} -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
        if [ ! -z "$external_ip" ] && [ "$external_ip" != "null" ]; then
          echo "Load balancer IP assigned: $external_ip"
          exit 0
        fi
        echo "Waiting for load balancer IP... ($elapsed/$timeout seconds)"
        sleep $interval
        elapsed=$((elapsed + interval))
      done
      
      echo "Timeout waiting for load balancer IP"
      exit 1
    EOT
  }
}

# Data source to get the final load balancer IP
data "kubernetes_service" "nginx_service" {
  depends_on = [null_resource.wait_for_load_balancer]
  
  metadata {
    name      = "${var.release_name}-controller"
    namespace = var.namespace
  }
}

