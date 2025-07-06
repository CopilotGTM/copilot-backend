########################
#  Azure DevOps agent pool
########################
resource "azuredevops_agent_pool" "vmss_pool" {
  name           = var.agent_pool_name          # e.g. "EphemeralAgentPool"
  auto_provision = true                         # VMSS-backed pool
  auto_update    = true                         # agents self-update
}

########################
#  Linux VM Scale Set
########################
resource "azurerm_linux_virtual_machine_scale_set" "agent_vmss" {
  name                = var.vmss_name
  resource_group_name = var.resource_group_name
  location            = var.location

  sku                 = var.vm_sku              # e.g. "Standard_D2ds_v4"
  instances           = var.min_instances       # 0 is fine for idle pools
  overprovision       = false                   # avoid extra VMs at start-up

  admin_username      = var.admin_username
  admin_password      = var.admin_password
  disable_password_authentication = false

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  os_disk {
    storage_account_type = "Standard_LRS"
    caching              = "ReadWrite"
  }

  network_interface {
    name    = "eth0"
    primary = true
    ip_configuration {
      name      = "internal"
      primary   = true
      subnet_id = var.subnet_id
    }
  }

  ###############################
  # Azure Pipelines VM extension
  ###############################
  extension {
    name                       = "AzurePipelinesAgent"
    publisher                  = "Microsoft.VisualStudio.Services"
    type                       = "TeamServicesAgentLinux"
    type_handler_version       = "1.0"
    auto_upgrade_minor_version = true

    settings = jsonencode({
      AzureDevOpsOrganizationUrl = var.org_service_url      # full URL
      PoolName                   = azuredevops_agent_pool.vmss_pool.name
      AgentName                  = var.agent_name_prefix
      AgentMajorVersion          = "3"                      # force latest agent
      enableAgentUpdate          = true
    })

    protected_settings = jsonencode({
      pat = var.azuredevops_pat
    })
  }

  # Optional – install Docker
  custom_data = base64encode(<<EOF
#!/usr/bin/env bash
set -e
apt-get update
apt-get install -y docker.io
systemctl enable --now docker
usermod -aG docker ${var.admin_username}
EOF
  )

  tags = {
    environment = "devops-vmss-agent"
    project     = var.project_tag
  }
}

########################
#  Project-level queue
########################
data "azuredevops_project" "this" {
  name = var.devops_project_name
}

resource "azuredevops_agent_queue" "vmss_queue" {
  project_id    = data.azuredevops_project.this.id
  agent_pool_id = azuredevops_agent_pool.vmss_pool.id
}
