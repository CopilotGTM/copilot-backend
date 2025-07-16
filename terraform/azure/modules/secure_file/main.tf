resource "azuredevops_serviceendpoint_kubernetes" "kubeconfig" {
  project_id            = var.devops_project_id
  service_endpoint_name = var.secure_file_name
  apiserver_url         = var.apiserver_url
  authorization_type    = "Kubeconfig"
  kubeconfig {
    kube_config            = var.secure_file_content
    accept_untrusted_certs = true
    cluster_context        = var.cluster_context
  }
}
