resource "azuredevops_serviceendpoint_dockerregistry" "acr" {
  project_id            = var.devops_project_id
  service_endpoint_name = var.service_endpoint_name

  docker_registry       = var.acr_login_server
  docker_username       = var.acr_sp_app_id
  docker_password       = var.acr_sp_app_secret
  registry_type         = "Others"
}