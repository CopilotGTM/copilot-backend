output "vmss_agent_pool_id" {
  description = "The ID of the Azure DevOps agent pool created for the VMSS."
  value       = azuredevops_agent_pool.vmss_pool.id
}

output "vmss_agent_pool_name" {
  description = "The name of the Azure DevOps agent pool for the VMSS."
  value       = azuredevops_agent_pool.vmss_pool.name
}

output "vmss_agent_queue_id" {
    description = "The ID of the agent queue associated with the VMSS pool."
    value = azuredevops_agent_queue.vmss_queue.id
}