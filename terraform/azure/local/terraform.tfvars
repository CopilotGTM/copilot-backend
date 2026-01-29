# -----------------------------------------------------------------------------
# Environment / Azure subscription
# -----------------------------------------------------------------------------
env             = "local"                                # Deployment environment name (e.g., dev/stage/prod/local)
subscription_id = "675df0ed-8e06-441b-a1e4-110d19521816" # Target Azure subscription (GUID)

# -----------------------------------------------------------------------------
# Resource Group / Region
# -----------------------------------------------------------------------------
resource_group_name = "copilot-gtm-app" # Resource group that will contain the resources
location            = "Central India"   # Primary Azure region for resources

# -----------------------------------------------------------------------------
# Project naming
# -----------------------------------------------------------------------------
project_name = "copilotgtm" # Project/app identifier used in naming conventions

# -----------------------------------------------------------------------------
# Database (PostgreSQL)
# -----------------------------------------------------------------------------
db_location = "Central India"         # Region for the database (can differ from `location` if needed)
db_name     = "copilotgtm-postgresdb" # PostgreSQL database/server name
# -----------------------------------------------------------------------------
# Network Security
# -----------------------------------------------------------------------------
public_ip_range_start = "0.0.0.0"
public_ip_range_end   = "255.255.255.255"
allowed_public_ips    = []

# -----------------------------------------------------------------------------
# Teams Bot
# -----------------------------------------------------------------------------
teams_bot_messaging_endpoint = "https://caitlyn-nonarmigerous-uninstructively.ngrok-free.dev/users/bots/teams/messages"

