"""Lock the read-only API contract for backend-owned person display names."""

from uuid import uuid4

from src.api.schemas.chat_models import ChatSharedByOut
from src.api.schemas.crm_account_models import AccountDetailContactItem
from src.api.schemas.crm_contact_models import ContactListItem, ContactMutationItem
from src.api.schemas.crm_deal_write_models import DealWriteResponse
from src.api.schemas.crm_email_models import CRMEmailItem
from src.api.schemas.crm_import_models import CRMImportRowItem
from src.api.schemas.crm_models import AvailableContactItem, DealOwnerDetail
from src.api.schemas.crm_note_models import NoteAuthor
from src.api.schemas.crm_participant_models import DealParticipantItem
from src.api.schemas.crm_user_models import CRMUserListItem
from src.api.schemas.email_name_fallback import EmailNameFallbackModel
from src.api.schemas.integrations.integration_schemas import ChannelMember
from src.api.schemas.upload_models import AttachmentUploaderResponse


def test_person_read_responses_share_backend_email_name_fallback() -> None:
    """Keep specialized GET DTOs on the shared backend fallback policy."""
    response_models = (
        AttachmentUploaderResponse,
        ChannelMember,
        ChatSharedByOut,
        ContactListItem,
        CRMEmailItem,
        CRMImportRowItem,
    )

    assert all(issubclass(model, EmailNameFallbackModel) for model in response_models)


def test_write_responses_do_not_apply_email_name_fallback() -> None:
    """Keep create/update responses exact instead of inferring presentation values."""
    write_response_models = (ContactMutationItem, DealParticipantItem, DealWriteResponse)

    assert not any(issubclass(model, EmailNameFallbackModel) for model in write_response_models)


def test_person_get_responses_derive_missing_display_name_from_email() -> None:
    """Give list, picker, nested-owner, and note-author GET DTOs one name policy."""
    assert CRMUserListItem(user_id=uuid4(), email="jane.doe@example.com").name == "Jane Doe"
    assert (
        ContactListItem(
            contact_id=uuid4(),
            full_name=None,
            email="jane.doe@example.com",
            job_title=None,
            phone=None,
            mobile_phone=None,
            linkedin_url=None,
            company=None,
            owner=None,
            last_contacted_at=None,
            last_activity_at=None,
            lifecycle_stage=None,
            lead_status=None,
            country=None,
            profile_enriched_at=None,
            created_at="2026-08-05T00:00:00Z",
            updated_at="2026-08-05T00:00:00Z",
        ).full_name
        == "Jane Doe"
    )
    assert (
        AccountDetailContactItem(
            contact_id=uuid4(),
            full_name=None,
            email="jane.doe@example.com",
            job_title=None,
        ).full_name
        == "Jane Doe"
    )
    assert DealOwnerDetail(email="jane.doe@example.com").name == "Jane Doe"
    assert NoteAuthor(email="jane.doe@example.com").name == "Jane Doe"


def test_person_get_response_derives_first_and_last_name_from_email() -> None:
    """Populate split-name GET fields when only an email is available."""
    contact = AvailableContactItem(contact_id=uuid4(), email="jane.doe@example.com")

    assert contact.first_name == "Jane"
    assert contact.last_name == "Doe"
    assert contact.full_name == "Jane Doe"


def test_person_get_response_preserves_real_name() -> None:
    """Preserve an authoritative name instead of replacing it with an email guess."""
    user = CRMUserListItem(user_id=uuid4(), name="Dr. Jane Doe", email="jane.doe@example.com")

    assert user.name == "Dr. Jane Doe"


def test_person_get_response_preserves_name_without_email() -> None:
    """Pass an authoritative name through when the optional email is absent."""
    user = CRMUserListItem(user_id=uuid4(), name="Dr. Jane Doe", email=None)

    assert user.name == "Dr. Jane Doe"


def test_person_get_response_keeps_name_empty_without_usable_email() -> None:
    """Avoid inventing person names when neither a real name nor usable email exists."""
    assert CRMUserListItem(user_id=uuid4(), email=None).name is None
    assert CRMUserListItem(user_id=uuid4(), email="support@example.com").name is None
