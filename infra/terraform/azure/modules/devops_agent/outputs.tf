# -----------------------------------------------------------------------------
# DevOps Self-Hosted Agent VM Module - Outputs
# -----------------------------------------------------------------------------

output "vm_id" {
  description = "ID of the agent virtual machine."
  value       = azurerm_linux_virtual_machine.agent.id
}

output "vm_name" {
  description = "Name of the agent virtual machine."
  value       = azurerm_linux_virtual_machine.agent.name
}

output "private_ip_address" {
  description = "Private IP address of the agent VM."
  value       = azurerm_network_interface.agent.private_ip_address
}

output "public_ip_address" {
  description = "Public IP address of the agent VM (if enabled)."
  value       = var.enable_public_ip ? azurerm_public_ip.agent[0].ip_address : null
}

output "principal_id" {
  description = "Principal ID of the VM's managed identity."
  value       = azurerm_linux_virtual_machine.agent.identity[0].principal_id
}

output "agent_pool" {
  description = "Name of the Azure DevOps agent pool."
  value       = var.agent_pool
}

output "agent_name" {
  description = "Name of the registered Azure DevOps agent."
  value       = var.agent_name
}

output "network_interface_id" {
  description = "ID of the network interface."
  value       = azurerm_network_interface.agent.id
}
