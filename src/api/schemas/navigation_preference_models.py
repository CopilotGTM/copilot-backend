from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SettingsItem = Literal[
    "integrations",
    "forms",
    "crm_fields",
    "pipelines",
    "import_data",
    "agent_actions",
    "reminders",
    "shared_chats",
    "audio",
    "password",
]


class NavigationExpandedSections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: bool = True
    records: bool = True
    analytics: bool = True
    settings_workspace_setup: bool = True
    settings_data: bool = True
    settings_automation: bool = True
    settings_library: bool = True
    settings_account: bool = True
    settings_crm_fields: bool = False


class NavigationPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expanded_sections: NavigationExpandedSections = Field(
        default_factory=NavigationExpandedSections
    )
    last_settings_item: SettingsItem = "integrations"
    crm_fields_entity: Literal["lead", "deal"] = "deal"


class UpdateNavigationPreferencesRequest(NavigationPreferences):
    """Complete navigation state saved after a user interaction."""
