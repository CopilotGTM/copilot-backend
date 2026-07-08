"""Lead service package exports.

Keep these exports lazy so worker-safe submodule imports do not load API-only
lead ingestion services through package initialisation.
"""

from __future__ import annotations

from typing import Any

_EXPORTS = {
    "LeadConversionService": ("src.service.crm.leads.conversion_service", "LeadConversionService"),
    "LeadFormsService": ("src.service.crm.leads.form_management_service", "LeadFormsService"),
    "LeadFormsServiceError": (
        "src.service.crm.leads.form_management_service",
        "LeadFormsServiceError",
    ),
    "LeadIngestionKeyService": (
        "src.service.crm.leads.ingestion_key_service",
        "LeadIngestionKeyService",
    ),
    "LeadIngestionKeyServiceError": (
        "src.service.crm.leads.ingestion_key_service",
        "LeadIngestionKeyServiceError",
    ),
    "LeadParticipantService": (
        "src.service.crm.leads.participant_service",
        "LeadParticipantService",
    ),
    "LeadReadService": ("src.service.crm.leads.read_service", "LeadReadService"),
    "LeadServiceError": ("src.service.crm.leads.contracts", "LeadServiceError"),
    "LeadWriteService": ("src.service.crm.leads.write_service", "LeadWriteService"),
    "PrivateLeadIngestionAuthError": (
        "src.service.crm.leads.ingestion_auth_service",
        "PrivateLeadIngestionAuthError",
    ),
    "PrivateLeadIngestionAuthService": (
        "src.service.crm.leads.ingestion_auth_service",
        "PrivateLeadIngestionAuthService",
    ),
    "PrivateLeadIngestionService": (
        "src.service.crm.leads.private_ingestion_service",
        "PrivateLeadIngestionService",
    ),
    "PrivateLeadIngestionServiceError": (
        "src.service.crm.leads.private_ingestion_service",
        "PrivateLeadIngestionServiceError",
    ),
    "PublicLeadFormSubmissionService": (
        "src.service.crm.leads.form_management_service",
        "PublicLeadFormSubmissionService",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
