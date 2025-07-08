output "letsencrypt_clusterissuer_name" {
  description = "Name of the letsencrypt ClusterIssuer"
  value       = kubernetes_manifest.letsencrypt_clusterissuer.manifest["metadata"]["name"]
}

output "devapi_certificate_name" {
  description = "Name of the devapi Certificate"
  value       = kubernetes_manifest.devapi_cert.manifest["metadata"]["name"]
}

output "devapi_certificate_secret_name" {
  description = "Secret name for the devapi Certificate"
  value       = kubernetes_manifest.devapi_cert.manifest["spec"]["secretName"]
}
