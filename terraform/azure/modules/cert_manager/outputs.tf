output "cert_manager_release_name" {
  description = "The name of the cert-manager Helm release."
  value       = helm_release.cert_manager.name
}
