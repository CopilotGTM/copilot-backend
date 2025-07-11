# modules/cert-manager/outputs.tf

output "namespace" {
  description = "The namespace where cert-manager is installed"
  value       = kubernetes_namespace.cert_manager.metadata[0].name
}

output "release_name" {
  description = "The name of the Helm release"
  value       = helm_release.cert_manager.name
}

output "chart_version" {
  description = "The version of cert-manager chart installed"
  value       = helm_release.cert_manager.version
}

output "letsencrypt_prod_issuer_name" {
  description = "The name of the Let's Encrypt production issuer"
  value       = var.letsencrypt_prod_issuer_name
}

output "letsencrypt_staging_issuer_name" {
  description = "The name of the Let's Encrypt staging issuer"
  value       = var.create_staging_issuer ? var.letsencrypt_staging_issuer_name : null
}

output "certificates" {
  description = "Information about created certificates"
  value = var.certificates
}

output "cert_manager_webhook_ca_secret" {
  description = "The CA secret used by cert-manager webhook"
  value       = "${var.release_name}-webhook-ca"
}

output "cert_manager_ready" {
  description = "Indicates if cert-manager is ready (based on null_resource completion)"
  value       = null_resource.wait_for_cert_manager.id != null
}
