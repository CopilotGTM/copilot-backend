"""Owner assignment helpers for internal CRM deals."""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.crm.deals.contracts import DealOwnerChangeResult
from src.service.crm.deals.mutation_event_service import changed_field


class DealOwnerService:
    """Owns owner snapshot changes once the CRM user has been resolved."""

    def apply_owner(self, *, deal: CRMDeal, crm_user: CRMUser | None) -> DealOwnerChangeResult:
        """Set or clear owner fields on a loaded deal."""
        old_values = {
            "owner_id": deal.owner_id,
            "owner_name": deal.owner_name,
            "owner_email": deal.owner_email,
        }
        deal.owner_id = crm_user.user_id if crm_user else None
        deal.owner_name = crm_user.full_name if crm_user else None
        deal.owner_email = crm_user.email if crm_user else None

        changed_fields: dict[str, dict[str, Any]] = {}
        for name, old in old_values.items():
            if change := changed_field(old, getattr(deal, name)):
                changed_fields[name] = change

        return DealOwnerChangeResult(
            deal=deal,
            changed_fields=changed_fields,
            changed=bool(changed_fields),
        )
