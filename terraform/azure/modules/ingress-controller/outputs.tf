output "nginx_ingress_public_ip" {
  description = "The static public IP assigned to the ingress controller"
  value       = azurerm_public_ip.nginx_ip.ip_address
}