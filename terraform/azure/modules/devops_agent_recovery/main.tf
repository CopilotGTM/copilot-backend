# -----------------------------------------------------------------------------
# DevOps Agent Spot VM Recovery Module
# Automatically restarts spot VMs that get deallocated due to eviction.
#
# How it works:
# 1. Azure Automation Account runs a PowerShell runbook on a schedule
# 2. The runbook checks if the target VM is deallocated
# 3. If deallocated, it starts the VM (the agent service auto-starts via systemd)
# 4. The VM re-joins the Azure DevOps agent pool automatically
# -----------------------------------------------------------------------------

resource "azurerm_automation_account" "recovery" {
  name                = var.automation_account_name
  location            = var.location
  resource_group_name = var.resource_group_name
  sku_name            = "Basic"

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}

# Grant the automation account permission to start the VM
resource "azurerm_role_assignment" "vm_contributor" {
  scope                = var.vm_id
  role_definition_name = "Virtual Machine Contributor"
  principal_id         = azurerm_automation_account.recovery.identity[0].principal_id
}

# PowerShell runbook that checks and restarts the VM
resource "azurerm_automation_runbook" "restart_spot_vm" {
  name                    = "Restart-SpotAgentVM"
  location                = var.location
  resource_group_name     = var.resource_group_name
  automation_account_name = azurerm_automation_account.recovery.name
  log_verbose             = false
  log_progress            = false
  runbook_type            = "PowerShell"

  content = <<-POWERSHELL
    <#
      .SYNOPSIS
        Restarts a deallocated Azure Spot VM used as a DevOps agent.
      .DESCRIPTION
        Checks the power state of the target VM. If deallocated (due to spot
        eviction), restarts it. The DevOps agent service starts automatically
        via systemd on boot and re-joins the agent pool.
    #>

    try {
        # Authenticate using the Automation Account's managed identity
        Connect-AzAccount -Identity | Out-Null

        $resourceGroup = "${var.resource_group_name}"
        $vmName        = "${var.vm_name}"

        # Get current VM status
        $vm = Get-AzVM -ResourceGroupName $resourceGroup -Name $vmName -Status
        $powerState = ($vm.Statuses | Where-Object { $_.Code -like "PowerState/*" }).Code

        Write-Output "VM '$vmName' power state: $powerState"

        if ($powerState -eq "PowerState/deallocated") {
            Write-Output "VM is deallocated (spot eviction). Starting VM..."
            Start-AzVM -ResourceGroupName $resourceGroup -Name $vmName
            Write-Output "VM '$vmName' started successfully. Agent will auto-register on boot."
        }
        elseif ($powerState -eq "PowerState/stopped") {
            Write-Output "VM is stopped. Starting VM..."
            Start-AzVM -ResourceGroupName $resourceGroup -Name $vmName
            Write-Output "VM '$vmName' started successfully."
        }
        else {
            Write-Output "VM is in state '$powerState'. No action needed."
        }
    }
    catch {
        Write-Error "Failed to check/restart VM: $_"
        throw
    }
  POWERSHELL

  tags = var.tags
}

# Schedule to run the runbook at the configured interval
resource "azurerm_automation_schedule" "spot_recovery" {
  name                    = "spot-vm-recovery-schedule"
  resource_group_name     = var.resource_group_name
  automation_account_name = azurerm_automation_account.recovery.name
  frequency               = "Hour"
  interval                = 1
  description             = "Checks every hour if the spot agent VM is deallocated and restarts it."

  lifecycle {
    ignore_changes = [start_time]
  }
}

# Link the schedule to the runbook
resource "azurerm_automation_job_schedule" "spot_recovery" {
  resource_group_name     = var.resource_group_name
  automation_account_name = azurerm_automation_account.recovery.name
  schedule_name           = azurerm_automation_schedule.spot_recovery.name
  runbook_name            = azurerm_automation_runbook.restart_spot_vm.name
}
