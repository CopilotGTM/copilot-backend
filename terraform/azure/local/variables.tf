# -----------------------------------------------------------------------------
# Project / Environment
# -----------------------------------------------------------------------------
variable "project_name" {
  description = "The project tag to apply across resources."
  type        = string
}
variable "env" {
  description = "Environment identifier (e.g., dev, prod, staging)."
  type        = string
  default     = "local"
}

# -----------------------------------------------------------------------------
# Azure Provider / Subscription
# -----------------------------------------------------------------------------
variable "subscription_id" {
  description = "The Azure subscription ID to use."
  type        = string
}

# ----------------------------------------------------------------------------
# Resource Group
# ----------------------------------------------------------------------------
variable "location" {
  description = "The Azure region for the Resource Group."
  type        = string
  default     = "Central India"
}
variable "resource_group_name" {
  description = "The base name of the Azure Resource Group to create."
  type        = string
}

# ----------------------------------------------------------------------------
# Database (PostgreSQL)
# ----------------------------------------------------------------------------
variable "db_location" {
  description = "The Azure region for the PostgreSQL database."
  type        = string
}
variable "db_name" {
  description = "The name of the PostgreSQL database."
  type        = string
}
# -----------------------------------------------------------------------------
# Network Security
# -----------------------------------------------------------------------------
variable "public_ip_range_start" {
  description = "Start IP address for the public allowlist range."
  type        = string
}

variable "public_ip_range_end" {
  description = "End IP address for the public allowlist range."
  type        = string
}

variable "allowed_public_ips" {
  description = "List of specific public IPs/CIDRs to allow (for future use)."
  type        = list(string)
  default     = []
}

# -----------------------------------------------------------------------------
# Teams Bot
# -----------------------------------------------------------------------------
variable "teams_bot_messaging_endpoint" {
  description = <<-DESC
    The HTTPS endpoint where the bot receives messages from Teams.

    For local development: Use ngrok URL (e.g., https://xxx.ngrok-free.app/users/bots/teams/messages)
  DESC
  type        = string
}

