output "cert_manager_release_name" {
  description = "The name of the cert-manager Helm release."
  value       = helm_release.cert_manager.name
}

output "letsencrypt_clusterissuer_name" {
  description = "The name of the Let's Encrypt ClusterIssuer."
  value       = kubernetes_manifest.letsencrypt_clusterissuer.manifest["metadata"]["name"]
}

output "devapi_certificate_name" {
  description = "The name of the devapi certificate."
  value       = kubernetes_manifest.devapi_cert.manifest["metadata"]["name"]
}

output "devapi_certificate_secret_name" {
  description = "The name of the secret created by the devapi certificate."
  value       = kubernetes_manifest.devapi_cert.manifest["spec"]["secretName"]
}
