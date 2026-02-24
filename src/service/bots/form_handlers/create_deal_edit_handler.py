"""Form handler for deal creation edit action.

Processes "create_deal_edit" actions from the confirmation preview card.
Consumes the original nonce, queries pipelines/stages for dropdown choices,
builds an editable FormDefinition pre-filled with the deal data, and returns
the rendered Adaptive Card via card_payload for in-place replacement.

The editable form submits back to "create_deal_submit" (reuses existing handler).
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlmodel import col
from src.adapters.card_builders import FormDefinition, FormField
from src.adapters.card_builders.teams_card_builder import TeamsFormCardBuilder
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
)
from src.infra.utils.logger import get_logger
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.form_handlers.nonce import create_nonce, validate_and_consume_nonce

logger = get_logger(__name__)


class CreateDealEditFormHandler(BaseFormHandler):
    """Handle create_deal_edit — transform preview card into an editable form."""

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        """Consume original nonce, build editable form, return as card_payload."""
        # Validate and consume the original nonce
        nonce = form_data.get("nonce", "")
        if not nonce or not validate_and_consume_nonce("deal_confirm", nonce):
            return FormHandlerResult(
                success=False,
                message=(
                    "This confirmation has expired or was already used. "
                    "Please ask the assistant to send a new one."
                ),
            )

        # Resolve user from Teams identity (need org_id for pipeline query)
        from_user = activity.get("from", {})
        teams_user_id = from_user.get("id", "")
        channel_data = activity.get("channelData", {})
        teams_tenant_id = channel_data.get("tenant", {}).get("id", "")

        from src.domain.models.master_tables.external_identity_models import (
            ExternalIdentityProvider,
        )
        from src.infra.database.sessions import (
            acquire_master_async_session,
            acquire_org_async_session,
        )
        from src.service.bots.identity_service import ExternalIdentityService

        identity_service = ExternalIdentityService()

        user = None
        async for master_session in acquire_master_async_session():
            user, _ = await identity_service.get_user_for_external_identity(
                master_session,
                ExternalIdentityProvider.TEAMS,
                teams_user_id,
                teams_tenant_id,
            )
            break

        if not user or not user.organisation_id:
            return FormHandlerResult(
                success=False,
                message="Could not identify your account. Please sign in first.",
            )

        org_id = user.organisation_id

        # Query pipelines/stages for dropdown choices
        pipeline_choices: list[dict[str, str]] = []
        stage_choices: list[dict[str, str]] = []
        default_pipeline_id: str | None = None

        pipeline_c = cast(Any, CRMPipeline).__table__.c
        stage_c = cast(Any, CRMDealStage).__table__.c

        async for org_session in acquire_org_async_session(org_id):
            try:
                pipeline_stmt = (
                    select(
                        col(CRMPipeline.pipeline_id),
                        col(CRMPipeline.name),
                        col(CRMPipeline.is_default),
                    )
                    .where(
                        pipeline_c.organisation_id == org_id,
                        pipeline_c.is_deleted.is_(False),
                        pipeline_c.is_active.is_(True),
                    )
                    .order_by(
                        pipeline_c.display_order.asc().nulls_last(),
                        pipeline_c.name.asc(),
                    )
                )
                pipeline_result = await org_session.execute(pipeline_stmt)
                pipeline_rows = pipeline_result.all()

                for p in pipeline_rows:
                    pid = str(p.pipeline_id)
                    pipeline_choices.append({"title": p.name, "value": pid})
                    if p.is_default or default_pipeline_id is None:
                        default_pipeline_id = pid

                if pipeline_rows:
                    pipeline_ids = [p.pipeline_id for p in pipeline_rows]
                    stage_stmt = (
                        select(
                            col(CRMDealStage.stage_id),
                            col(CRMDealStage.pipeline_id),
                            col(CRMDealStage.name),
                        )
                        .where(
                            stage_c.pipeline_id.in_(pipeline_ids),
                            stage_c.is_deleted.is_(False),
                        )
                        .order_by(
                            stage_c.display_order.asc().nulls_last(),
                            stage_c.name.asc(),
                        )
                    )
                    stage_result = await org_session.execute(stage_stmt)
                    stage_rows = stage_result.all()

                    pipeline_names: dict[str, str] = {
                        str(p.pipeline_id): p.name for p in pipeline_rows
                    }
                    multiple_pipelines = len(pipeline_rows) > 1

                    for s in stage_rows:
                        title = (
                            f"{pipeline_names[str(s.pipeline_id)]} > {s.name}"
                            if multiple_pipelines
                            else s.name
                        )
                        stage_choices.append(
                            {
                                "title": title,
                                "value": f"{s.pipeline_id}|{s.stage_id}",
                            }
                        )
            except Exception as e:
                logger.exception("create_deal_edit: error querying pipelines/stages: %s", e)
            break

        # Create a new nonce for the editable form
        new_nonce = create_nonce("deal_confirm")

        # Pre-fill values from the original confirmation card data
        name_val = form_data.get("name", "")
        amount_val = form_data.get("amount")
        currency_val = form_data.get("currency", "USD")
        description_val = form_data.get("description", "")
        close_date_val = form_data.get("close_date", "")
        priority_val = form_data.get("priority", "")
        account_name_val = form_data.get("account_name", "")
        account_domain_val = form_data.get("account_domain", "")
        pipeline_id_val = form_data.get("pipeline_id", "") or default_pipeline_id

        # Build editable form fields
        fields: list[FormField] = [
            FormField(
                id="name",
                label="Deal Name",
                field_type="text",
                required=True,
                value=name_val or None,
                placeholder="e.g. Acme Corp - Enterprise License",
            ),
            FormField(
                id="amount",
                label="Amount",
                field_type="number",
                value=amount_val,
                placeholder="e.g. 50000",
                min_val=0,
            ),
            FormField(
                id="currency",
                label="Currency",
                field_type="text",
                value=currency_val or "USD",
                placeholder="e.g. USD, EUR, GBP",
            ),
        ]

        if pipeline_choices:
            fields.append(
                FormField(
                    id="pipeline_id",
                    label="Pipeline",
                    field_type="choice",
                    choices=pipeline_choices,
                    value=pipeline_id_val,
                )
            )

        if stage_choices:
            fields.append(
                FormField(
                    id="stage",
                    label="Stage",
                    field_type="choice",
                    choices=stage_choices,
                    placeholder="Select a stage",
                )
            )

        fields.extend(
            [
                FormField(
                    id="close_date",
                    label="Expected Close Date",
                    field_type="date",
                    value=close_date_val or None,
                ),
                FormField(
                    id="priority",
                    label="Priority",
                    field_type="choice",
                    choices=[
                        {"title": "Low", "value": "low"},
                        {"title": "Medium", "value": "medium"},
                        {"title": "High", "value": "high"},
                    ],
                    value=priority_val or None,
                    placeholder="Select priority",
                ),
                FormField(
                    id="account_name",
                    label="Account Name",
                    field_type="text",
                    value=account_name_val or None,
                    placeholder="e.g. Acme Corp",
                ),
                FormField(
                    id="account_domain",
                    label="Account Domain",
                    field_type="text",
                    value=account_domain_val or None,
                    placeholder="e.g. acme.com",
                ),
                FormField(
                    id="description",
                    label="Description",
                    field_type="text",
                    value=description_val or None,
                    placeholder="Brief description of the deal",
                    multiline=True,
                ),
            ]
        )

        form = FormDefinition(
            title="Edit Deal Details",
            subtitle="Modify the deal details below. Required fields are marked with *.",
            fields=fields,
            submit_action="create_deal_submit",
            submit_label="Create Deal",
            cancel_action="create_deal_cancel",
            cancel_label="Cancel",
            metadata={
                "nonce": new_nonce,
                "organisation_id": str(org_id),
            },
        )

        # Render via TeamsFormCardBuilder
        builder = TeamsFormCardBuilder()
        card_content = builder.build(form)

        # Extract the Adaptive Card content from the message payload —
        # card_content is a full message payload with attachments; we need
        # just the card content for the invoke response
        adaptive_card = card_content["attachments"][0]["content"]

        return FormHandlerResult(
            success=True,
            message="",
            card_payload=adaptive_card,
        )


__all__ = ["CreateDealEditFormHandler"]
