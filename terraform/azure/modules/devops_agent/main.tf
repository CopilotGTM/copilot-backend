# -----------------------------------------------------------------------------
# DevOps Self-Hosted Agent VM Module
# Creates a Linux VM in a private subnet with Azure DevOps agent pre-installed
# -----------------------------------------------------------------------------

# Network Interface
resource "azurerm_network_interface" "agent" {
  name                = "${var.vm_name}-nic"
  location            = var.location
  resource_group_name = var.resource_group_name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = var.enable_public_ip ? azurerm_public_ip.agent[0].id : null
  }

  tags = var.tags
}

# Public IP (optional - only if enable_public_ip is true)
resource "azurerm_public_ip" "agent" {
  count               = var.enable_public_ip ? 1 : 0
  name                = "${var.vm_name}-pip"
  location            = var.location
  resource_group_name = var.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"

  tags = var.tags
}

# Cloud-init script to install Azure DevOps agent and tools
locals {
  cloud_init = <<-EOF
#!/bin/bash
set -e

# Log everything
exec > >(tee /var/log/devops-agent-setup.log) 2>&1
echo "Starting DevOps agent setup at $(date)"

# Generic retry function
function retry_command {
  local -r cmd="$1"
  local -r max_attempts=5
  local -r sleep_seconds=10
  local attempt=1

  until $cmd; do
    echo "Command '$cmd' failed. Attempt $attempt of $max_attempts."
    if (( attempt == max_attempts )); then
      echo "Command '$cmd' failed after $max_attempts attempts. Exiting."
      return 1
    fi
    sleep $sleep_seconds
    ((attempt++))
  done
}

# Wait for network validation (hacky but effective for some Azure race conditions)
sleep 30

# Update system
retry_command "apt-get update"
# Attempt upgrade but don't fail hard if it locks
apt-get upgrade -y || echo "Upgrade failed or locked, skipping..."

# Install prerequisites
retry_command "apt-get install -y curl wget unzip jq git ca-certificates gnupg lsb-release apt-transport-https software-properties-common"

%{if var.install_azure_cli}
# Install Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | bash
%{endif}

%{if var.install_docker}
# Install Docker
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
retry_command "apt-get update"
retry_command "apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin"
usermod -aG docker ${var.admin_username}
systemctl enable docker
systemctl start docker
%{endif}

%{if var.install_terraform}
# Install Terraform
wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/hashicorp.list
retry_command "apt-get update"
retry_command "apt-get install -y terraform=${var.terraform_version}-1"
%{endif}

%{if var.install_kubectl}
# Install kubectl
mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /" | tee /etc/apt/sources.list.d/kubernetes.list
retry_command "apt-get update"
retry_command "apt-get install -y kubectl"
%{endif}

# Install Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Install Node.js (for some pipeline tasks)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
retry_command "apt-get install -y nodejs"

# Create agent directory
AGENT_DIR="/opt/azure-devops-agent"
mkdir -p $AGENT_DIR
cd $AGENT_DIR

# Download and extract Azure DevOps agent
echo "Resolving Agent Version..."
AGENT_VERSION=$(curl -s --retry 5 --retry-delay 5 https://api.github.com/repos/microsoft/azure-pipelines-agent/releases/latest | jq -r '.tag_name' | sed 's/v//')

if [ -z "$AGENT_VERSION" ] || [ "$AGENT_VERSION" == "null" ]; then
    echo "Failed to resolve agent version, falling back to 3.236.1"
    AGENT_VERSION="3.236.1"
fi

echo "Installing Azure DevOps agent version: $AGENT_VERSION"
retry_command "curl -fsSL https://download.agent.dev.azure.com/agent/$AGENT_VERSION/vsts-agent-linux-x64-$AGENT_VERSION.tar.gz -o agent.tar.gz"

tar xzf agent.tar.gz
rm agent.tar.gz

# Set ownership
chown -R ${var.admin_username}:${var.admin_username} $AGENT_DIR

# Configure the agent (run as the admin user)
echo "Configuring Agent..."
sudo -u ${var.admin_username} ./config.sh \
  --unattended \
  --url "${var.azuredevops_url}" \
  --auth pat \
  --token "${var.azuredevops_pat}" \
  --pool "${var.agent_pool}" \
  --agent "${var.agent_name}-$(hostname)" \
  --acceptTeeEula \
  --replace

# Install and start the agent as a service
./svc.sh install ${var.admin_username}
./svc.sh start

echo "DevOps agent setup completed at $(date)"
echo "Agent registered to pool '${var.agent_pool}'"
EOF
}

# Linux Virtual Machine
resource "azurerm_linux_virtual_machine" "agent" {
  name                = var.vm_name
  location            = var.location
  resource_group_name = var.resource_group_name
  size                = var.vm_size
  admin_username      = var.admin_username

  # Use password auth if no SSH key provided
  disable_password_authentication = var.ssh_public_key != "" ? true : false
  admin_password                  = var.ssh_public_key == "" ? var.admin_password : null

  network_interface_ids = [azurerm_network_interface.agent.id]

  custom_data = base64encode(local.cloud_init)

  os_disk {
    name                 = "${var.vm_name}-osdisk"
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = var.os_disk_size_gb
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  # SSH key configuration (if provided)
  dynamic "admin_ssh_key" {
    for_each = var.ssh_public_key != "" ? [1] : []
    content {
      username   = var.admin_username
      public_key = var.ssh_public_key
    }
  }

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [
      custom_data, # Don't recreate VM if cloud-init changes
    ]
  }
}

# Network Security Group for the agent
resource "azurerm_network_security_group" "agent" {
  name                = "${var.vm_name}-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name

  # Allow outbound HTTPS (for Azure DevOps, Docker Hub, etc.)
  security_rule {
    name                       = "AllowHTTPSOutbound"
    priority                   = 100
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # Allow outbound HTTP (for package managers)
  security_rule {
    name                       = "AllowHTTPOutbound"
    priority                   = 110
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # Allow SSH inbound (only if public IP is enabled)
  dynamic "security_rule" {
    for_each = var.enable_public_ip ? [1] : []
    content {
      name                       = "AllowSSHInbound"
      priority                   = 100
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "22"
      source_address_prefix      = "*"
      destination_address_prefix = "*"
    }
  }

  tags = var.tags
}

# Associate NSG with NIC
resource "azurerm_network_interface_security_group_association" "agent" {
  network_interface_id      = azurerm_network_interface.agent.id
  network_security_group_id = azurerm_network_security_group.agent.id
}
