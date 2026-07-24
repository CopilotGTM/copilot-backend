from __future__ import annotations

from types import SimpleNamespace

from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
)
from src.service.integrations.crm.records.salesforce.deals.participant_normalizer import (
    contact_reference_fields_from_definitions,
    extract_contact_reference_participants,
    extract_participants,
)


def test_normalizer_keeps_primary_as_flag_on_contact_and_emits_company_participant() -> None:
    extraction = extract_participants(
        [
            {
                "OpportunityId": "006A",
                "ContactId": "003A",
                "Role": "Decision Maker",
                "IsPrimary": True,
            }
        ],
        company_external_id_by_deal={"006A": "001COMP"},
    )

    roles = {
        (
            participant.participant_type,
            participant.related_external_id,
            participant.relationship_role,
        )
        for participant in extraction.participants
    }
    assert ("contact", "003A", "Decision Maker") in roles
    assert (
        len(
            [
                participant
                for participant in extraction.participants
                if participant.participant_type == "contact"
            ]
        )
        == 1
    )
    assert ("company", "001COMP", None) in roles
    assert extraction.primary_contact_by_deal["006A"] == "003A"
    assert extraction.contact_count_by_deal["006A"] == 1
    assert extraction.associations[0].association_type_id == "OpportunityContactRole"


def test_contact_reference_primary_survives_nonprimary_contact_role() -> None:
    extraction = extract_participants(
        [
            {
                "OpportunityId": "006A",
                "ContactId": "003A",
                "Role": "Decision Maker",
                "IsPrimary": False,
            }
        ],
        company_external_id_by_deal={},
        additional_primary_contact_by_deal={"006A": "003A"},
    )

    assert all(participant.is_primary for participant in extraction.participants)
    assert extraction.primary_contact_by_deal == {"006A": "003A"}


def test_contact_reference_fields_use_salesforce_reference_metadata() -> None:
    fields = contact_reference_fields_from_definitions(
        [
            SimpleNamespace(
                provider_field_key="Primary_Contact_Person__c",
                label="Primary Contact Person",
                data_payload={
                    "name": "Primary_Contact_Person__c",
                    "label": "Primary Contact Person",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "queryable": True,
                },
            ),
            SimpleNamespace(
                provider_field_key="Email_ID__c",
                label="Email",
                data_payload={
                    "name": "Email_ID__c",
                    "type": "email",
                    "referenceTo": [],
                    "queryable": True,
                },
            ),
        ]
    )

    assert len(fields) == 1
    assert fields[0].field_name == "Primary_Contact_Person__c"
    assert fields[0].relationship_role == "Primary Contact Person"
    assert fields[0].is_primary is True
    assert fields[0].decision_power == DecisionPower.UNKNOWN


def test_contact_reference_fields_aggregate_same_contact_with_bounded_hints() -> None:
    fields = contact_reference_fields_from_definitions(
        [
            SimpleNamespace(
                provider_field_key="Primary_Contact_Person__c",
                label="Primary Contact Person",
                data_payload={
                    "name": "Primary_Contact_Person__c",
                    "label": "Primary Contact Person",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "queryable": True,
                },
            ),
            SimpleNamespace(
                provider_field_key="Decision_Maker__c",
                label="Decision Maker",
                data_payload={
                    "name": "Decision_Maker__c",
                    "label": "Decision Maker",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "queryable": True,
                },
            ),
            SimpleNamespace(
                provider_field_key="Primary_Budget_Owner__c",
                label="Primary Budget Owner",
                data_payload={
                    "name": "Primary_Budget_Owner__c",
                    "label": "Primary Budget Owner",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "queryable": True,
                },
            ),
        ]
    )

    extraction = extract_contact_reference_participants(
        [
            {
                "Id": "006HEROX",
                "Primary_Contact_Person__c": "003SHEKHAR",
                "Decision_Maker__c": "003SHEKHAR",
                "Primary_Budget_Owner__c": "003SHEKHAR",
            }
        ],
        fields=fields,
    )

    assert len(extraction.participants) == 1
    participant = extraction.participants[0]
    assert participant.related_external_id == "003SHEKHAR"
    assert participant.is_primary is True
    assert participant.decision_power == DecisionPower.DECISION_MAKER.value
    assert participant.stance == StakeholderStance.UNKNOWN.value
    assert participant.relationship_role == "Primary Budget Owner"
    assert participant.data_payload == {
        "source": "Opportunity contact reference",
        "opportunity_id": "006HEROX",
        "contact_id": "003SHEKHAR",
        "field_names": [
            "Decision_Maker__c",
            "Primary_Budget_Owner__c",
            "Primary_Contact_Person__c",
        ],
    }
    assert extraction.primary_contact_by_deal == {"006HEROX": "003SHEKHAR"}
    assert extraction.contact_count_by_deal == {"006HEROX": 1}


def test_contact_reference_semantics_map_champion_and_unknown_fields() -> None:
    fields = contact_reference_fields_from_definitions(
        [
            SimpleNamespace(
                provider_field_key="Champion_Contact__c",
                label="Champion Contact",
                data_payload={
                    "name": "Champion_Contact__c",
                    "label": "Champion Contact",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "queryable": True,
                },
            ),
            SimpleNamespace(
                provider_field_key="Implementation_Contact__c",
                label="Implementation Contact",
                data_payload={
                    "name": "Implementation_Contact__c",
                    "label": "Implementation Contact",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "queryable": True,
                },
            ),
        ]
    )

    assert fields[0].stance == StakeholderStance.CHAMPION
    assert fields[1].decision_power == DecisionPower.UNKNOWN
    assert fields[1].stance == StakeholderStance.UNKNOWN


def test_economic_buyer_contact_reference_does_not_infer_decision_power() -> None:
    fields = contact_reference_fields_from_definitions(
        [
            SimpleNamespace(
                provider_field_key="Economic_Buyer__c",
                label="Economic Buyer",
                data_payload={
                    "name": "Economic_Buyer__c",
                    "label": "Economic Buyer",
                    "type": "reference",
                    "referenceTo": ["Contact"],
                    "queryable": True,
                },
            )
        ]
    )

    assert fields[0].relationship_role == "Economic Buyer"
    assert fields[0].decision_power == DecisionPower.UNKNOWN


def test_conflicting_primary_contact_fields_resolve_deterministically() -> None:
    definitions = [
        SimpleNamespace(
            provider_field_key="Z_Primary_Contact__c",
            data_payload={
                "name": "Z_Primary_Contact__c",
                "label": "Z Primary Contact",
                "type": "reference",
                "referenceTo": ["Contact"],
            },
        ),
        SimpleNamespace(
            provider_field_key="A_Primary_Contact__c",
            data_payload={
                "name": "A_Primary_Contact__c",
                "label": "A Primary Contact",
                "type": "reference",
                "referenceTo": ["Contact"],
            },
        ),
    ]
    raw = {
        "Id": "006A",
        "Z_Primary_Contact__c": "003Z",
        "A_Primary_Contact__c": "003A",
    }

    forward = extract_contact_reference_participants(
        [raw],
        fields=contact_reference_fields_from_definitions(definitions),
    )
    reverse = extract_contact_reference_participants(
        [raw],
        fields=contact_reference_fields_from_definitions(reversed(definitions)),
    )

    assert forward.primary_contact_by_deal == {"006A": "003A"}
    assert reverse.primary_contact_by_deal == {"006A": "003A"}
