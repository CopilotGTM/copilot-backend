# Azure DevOps Static Self-Hosted Agent Module
#
# Provisions a persistent VM and NIC to be used as a self-hosted Azure DevOps agent.
# VM can be started/stopped on-demand for cost-saving, e.g., via automation, Logic Apps, or a scheduled script.

resource "azurerm_network_interface" "agent_nic" {
  name                = "${var.name_prefix}-nic"
  location            = var.location
  resource_group_name = var.resource_group_name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_linux_virtual_machine" "agent_vm" {
  name                = "${var.name_prefix}-vm"
  resource_group_name = var.resource_group_name
  location            = var.location
  size                = "Standard_B1s"
  admin_username      = var.admin_username

  network_interface_ids = [
    azurerm_network_interface.agent_nic.id
  ]

  admin_password = var.admin_password
  disable_password_authentication = false

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    name                 = "${var.name_prefix}-osdisk"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-focal"
    sku       = "20_04-lts"
    version   = "latest"
  }

  tags = {
    environment = "devops-agent"
    purpose     = "static-devops-agent"
  }

  # cloud-init script to register self-hosted agent on provisioning
  custom_data = base64encode(<<EOF
#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl jq zip libcurl4 libunwind8 net-tools

# Fetch the latest agent version dynamically
AGENT_VERSION=$(curl -s https://api.github.com/repos/microsoft/azure-pipelines-agent/releases/latest | jq -r ".tag_name" | sed "s/^v//")
ORG_SERVICE_URL="${var.org_service_url}"
AZDO_PAT="${var.azuredevops_pat}"
AGENT_POOL="${var.agent_pool}"
AGENT_NAME="${var.agent_name}"

mkdir -p /opt/azagent && cd /opt/azagent
curl -L -o agent.tar.gz https://vstsagentpackage.azureedge.net/agent/$AGENT_VERSION/vsts-agent-linux-x64-$AGENT_VERSION.tar.gz
tar zxvf agent.tar.gz
./bin/installdependencies.sh
useradd -m azagent || true

# Register and configure the agent with supplied variables
./config.sh --unattended --url "$ORG_SERVICE_URL" --auth pat --token "$AZDO_PAT" --pool "$AGENT_POOL" --agent "$AGENT_NAME" --acceptTeeEula --replace
chown -R azagent:azagent /opt/azagent
sudo -u azagent ./svc.sh install
sudo -u azagent ./svc.sh start
EOF
  )
}
