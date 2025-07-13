output "nginx_ingress_public_ip" {
  description = "The public IP address of the NGINX ingress controller."
  value       = azurerm_public_ip.nginx_ip.ip_address
}

output "nginx_ingress_public_ip_id" {
  description = "The ID of the public IP address of the NGINX ingress controller."
  value       = azurerm_public_ip.nginx_ip.id
}