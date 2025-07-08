output "cert_manager_release_status" {
  description = "The status of the cert-manager Helm release."
  value       = helm_release.cert_manager.status
}
