"""
Load Testing Configuration

API endpoints and settings for load testing.
"""

# API Endpoints
ENDPOINTS = {
    # Health Check
    "health": "/health",

    # Authentication
    "login": "/users/auth/login",
    "refresh_token": "/users/auth/refresh-token",
    "logout": "/users/auth/logout",

    # CRM - Deals
    "deals": "/users/crm/deals",
    "deals_stats": "/users/crm/deals/stats",

    # CRM - Tasks (requires deal_id)
    "deal_tasks": "/users/crm/deals/{deal_id}/tasks",

    # CRM - Meetings (requires deal_id)
    "deal_meetings": "/users/crm/deals/{deal_id}/meetings",

    # CRM - Stakeholders (requires deal_id)
    "deal_stakeholders": "/users/crm/deals/{deal_id}/stakeholders",

    # CRM - Timeline (requires deal_id)
    "deal_timeline": "/users/crm/deals/{deal_id}/timeline",

    # Chat
    "chat_list": "/users/chats/list_chats",
    "chat_messages": "/users/chats/messages/list",
    "send_message": "/users/chats/send_message",

    # Integrations
    "integrations_available": "/users/integrations/available",
}

# Default test configuration
DEFAULT_CONFIG = {
    "wait_time_min": 1,  # Minimum wait between tasks (seconds)
    "wait_time_max": 3,  # Maximum wait between tasks (seconds)
}
