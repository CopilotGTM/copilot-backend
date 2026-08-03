# -----------------------------------------------------------------
# Resource Group Outputs
# -----------------------------------------------------------------
output "resource_group_name" {
  description = "The name of the created Azure Resource Group."
  value       = module.resource_group.resource_group_name
}

output "resource_group_location" {
  description = "The location of the created Azure Resource Group."
  value       = module.resource_group.resource_group_location
}

output "resource_group_id" {
  description = "The ID of the created Azure Resource Group."
  value       = module.resource_group.resource_group_id
}

# -----------------------------------------------------------------
# VNet Outputs
# -----------------------------------------------------------------
output "vnet_name" {
  description = "The name of the created Virtual Network."
  value       = module.vnet.vnet_name
}

output "vnet_id" {
  description = "The ID of the created Virtual Network."
  value       = module.vnet.vnet_id
}

output "vnet_address_space" {
  description = "The address space of the created Virtual Network."
  value       = module.vnet.address_space
}

output "public_subnet_ids" {
  description = "IDs of all public subnets."
  value       = module.vnet.public_subnet_ids
}

output "private_subnet_ids" {
  description = "IDs of all private subnets."
  value       = module.vnet.private_subnet_ids
}

output "public_subnet_names" {
  description = "Names of all public subnets."
  value       = module.vnet.public_subnet_names
}

output "private_subnet_names" {
  description = "Names of all private subnets."
  value       = module.vnet.private_subnet_names
}

output "public_subnet_address_prefixes" {
  description = "Address prefixes of all public subnets."
  value       = module.vnet.public_subnet_address_prefixes
}

output "private_subnet_address_prefixes" {
  description = "Address prefixes of all private subnets."
  value       = module.vnet.private_subnet_address_prefixes
}

output "nat_gateway_public_ip" {
  description = "The public IP address of the NAT Gateway."
  value       = module.vnet.nat_gateway_public_ip
}

# -----------------------------------------------------------------
# ACR Outputs
# -----------------------------------------------------------------

output "acr_login_server" {
  description = "The login server URL of the Azure Container Registry."
  value       = module.acr.acr_login_server
}
output "acr_id" {
  description = "The ID of the Azure Container Registry."
  value       = module.acr.acr_id
}


# -----------------------------------------------------------------
# AKS Outputs
# -----------------------------------------------------------------

output "aks_name" {
  description = "The name of the AKS cluster."
  value       = module.aks.aks_name
}
output "aks_id" {
  description = "The Azure resource ID of the AKS cluster."
  value       = module.aks.aks_id
}
output "aks_kube_config" {
  description = "Kube config to connect to the AKS cluster."
  value       = module.aks.kube_config
  sensitive   = true
}

output "aks_host" {
  description = "The Kubernetes cluster API server address."
  value       = module.aks.host
  sensitive   = true
}

output "aks_client_certificate" {
  description = "The client certificate for authenticating to the cluster."
  value       = module.aks.client_certificate
  sensitive   = true
}

output "aks_client_key" {
  description = "The client key for authenticating to the cluster."
  value       = module.aks.client_key
  sensitive   = true
}

output "aks_cluster_ca_certificate" {
  description = "The cluster CA certificate."
  value       = module.aks.cluster_ca_certificate
  sensitive   = true
}

output "aks_principal_id" {
  description = "The managed identity principal ID of the AKS cluster."
  value       = module.aks.aks_principal_id
}

output "aks_node_resource_group" {
  description = "The AKS managed node resource group."
  value       = module.aks.node_resource_group
}

output "envoy_gateway_public_ip" {
  description = "Static public IP address dedicated to prod Envoy Gateway."
  value       = try(azurerm_public_ip.envoy_gateway[0].ip_address, null)
}


# -----------------------------------------------------------------
# Redis Outputs
# -----------------------------------------------------------------

output "redis_hostname" {
  description = "The hostname of the Redis cache."
  value       = module.redis.hostname
}

output "redis_primary_access_key" {
  description = "The primary access key for the Redis cache."
  value       = module.redis.primary_access_key
  sensitive   = true
}

output "redis_port" {
  description = "The SSL port for the Redis cache."
  value       = module.redis.port
}


# # -----------------------------------------------------------------
# # Service Connection Outputs
# # -----------------------------------------------------------------
# output "service_connection_id" {
#   description = "Azure DevOps service connection ID used for ACR and AKS."
#   value       = module.service_connection.service_connection_id
# }

# output "service_connection_name" {
#   description = "Azure DevOps service connection name."
#   value       = module.service_connection.service_connection_name
# }

# output "service_connection_subscription_id" {
#   description = "Azure Subscription ID associated with service connection."
#   value       = module.service_connection.subscription_id
# }

# -----------------------------------------------------------------
# Workload Identity Outputs
# -----------------------------------------------------------------

output "workload_identity_backend_client_id" {
  description = "Client ID for backend workload identity (use for azure.workload.identity/client-id annotation)."
  value       = module.workload_identity_backend.client_id
}

output "workload_identity_backend_object_id" {
  description = "Service principal object ID for backend workload identity (used in role assignments)."
  value       = module.workload_identity_backend.object_id
}

output "workload_identity_workers_client_id" {
  description = "Client ID for workers workload identity (use for azure.workload.identity/client-id annotation)."
  value       = module.workload_identity_workers.client_id
}

output "workload_identity_workers_object_id" {
  description = "Service principal object ID for workers workload identity (used in role assignments)."
  value       = module.workload_identity_workers.object_id
}


# -----------------------------------------------------------------
# Workload Identity / Tenant Diagnostics
# -----------------------------------------------------------------

output "aks_oidc_issuer_url" {
  description = "OIDC issuer URL for the AKS cluster (contains tenant ID)."
  value       = module.aks.oidc_issuer_url
}


output "azuread_provider_tenant_id" {
  description = "Tenant ID used by the AzureAD provider (validate matches AKS OIDC issuer tenant)."
  value       = data.azuread_client_config.current.tenant_id
}

# -----------------------------------------------------------------
# Key Vault Validation Outputs (to confirm correct tenant vaults)
# -----------------------------------------------------------------

output "master_key_vault_uri" {
  description = "URI of the master Key Vault (should belong to active tenant)."
  value       = module.master_key_vault.key_vault_uri
}


/*
# -----------------------------------------------------------------
# Legacy DevOps Agent VM Outputs
# -----------------------------------------------------------------
# Static runner outputs stay commented while module.devops_agent is commented.
output "devops_agent_vm_name" {
  description = "Name of the self-hosted DevOps agent VM."
  value       = module.devops_agent.vm_name
}

output "devops_agent_private_ip" {
  description = "Private IP address of the DevOps agent VM."
  value       = module.devops_agent.private_ip_address
}

output "devops_agent_pool" {
  description = "Name of the Azure DevOps agent pool."
  value       = module.devops_agent.agent_pool
}

output "devops_agent_name" {
  description = "Name of the registered Azure DevOps agent."
  value       = module.devops_agent.agent_name
}
*/

# -----------------------------------------------------------------
# Managed DevOps Pool Outputs
# -----------------------------------------------------------------
output "managed_devops_prod_deploy_pool_name" {
  description = "Name of the Managed DevOps Pool used for prod deployment jobs."
  value       = try(module.managed_devops_prod_deploy_pool[0].name, null)
}

output "managed_devops_prod_deploy_pool_id" {
  description = "Resource ID of the Managed DevOps Pool used for prod deployment jobs."
  value       = try(module.managed_devops_prod_deploy_pool[0].id, null)
}
