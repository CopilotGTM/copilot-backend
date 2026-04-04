# -----------------------------------------------------------------------------
# DevOps Agent Spot VM Recovery Module - Outputs
# -----------------------------------------------------------------------------

output "automation_account_id" {
  description = "ID of the Azure Automation Account."
  value       = azurerm_automation_account.recovery.id
}

output "automation_account_name" {
  description = "Name of the Azure Automation Account."
  value       = azurerm_automation_account.recovery.name
}

output "automation_principal_id" {
  description = "Principal ID of the Automation Account's managed identity."
  value       = azurerm_automation_account.recovery.identity[0].principal_id
}

output "runbook_name" {
  description = "Name of the recovery runbook."
  value       = azurerm_automation_runbook.restart_spot_vm.name
}
