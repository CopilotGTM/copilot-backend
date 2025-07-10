output "nginx_ingress_public_ip" {
  description = "The static public IP assigned to the ingress controller"
  value       = azurerm_public_ip.nginx_ip.ip_address
}

# Output useful information for debugging
output "nginx_static_ip" {
  value = azurerm_public_ip.nginx_ip.ip_address
}

output "nginx_load_balancer_ip" {
  value = try(data.kubernetes_service.nginx_service.status.0.load_balancer.0.ingress.0.ip, "Not assigned yet")
}

output "helm_release_status" {
  value = helm_release.nginx.status
}

output "namespace_name" {
  value = kubernetes_namespace.ingress.metadata[0].name
}