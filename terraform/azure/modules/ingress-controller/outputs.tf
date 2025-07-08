output "nginx_ingress_release_name" {
  description = "The name of the Helm release for nginx ingress controller"
  value       = helm_release.nginx_ingress.name
}

output "nginx_ingress_namespace" {
  description = "The namespace where nginx ingress controller is deployed"
  value       = helm_release.nginx_ingress.namespace
}

output "nginx_ingress_status" {
  description = "Helm release status"
  value       = helm_release.nginx_ingress.status
}

output "nginx_ingress_public_ip" {
  description = "The public IP address of the NGINX ingress controller LoadBalancer service"
  value       = data.kubernetes_service.nginx_ingress.status[0].load_balancer[0].ingress[0].ip
}
