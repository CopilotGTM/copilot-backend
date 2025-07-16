output "secure_file_id" {
  description = "The ID of the secure file."
  value       = azuredevops_serviceendpoint_kubernetes.kubeconfig.id
}
