output "vm_id" {
  value = azurerm_linux_virtual_machine.agent_vm.id
}

output "nic_id" {
  value = azurerm_network_interface.agent_nic.id
}

output "private_ip" {
  value = azurerm_network_interface.agent_nic.private_ip_address
}