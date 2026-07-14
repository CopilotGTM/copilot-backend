from src.domain.models.organisation_tables.sync.crm.deals import CRMStageState
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_normalizer import (
    normalize_salesforce_pipelines,
)


def test_normalize_salesforce_pipelines_derives_composite_stage_ids_and_stage_states() -> None:
    record_types = [
        {
            "Id": "012A",
            "Name": "Enterprise Sales",
            "DeveloperName": "Enterprise_Sales",
            "Description": "Enterprise pipeline",
            "IsActive": True,
            "IsMaster": False,
        }
    ]
    stage_values_by_record_type = {
        "012A": [
            {"value": "Prospecting", "label": "Prospecting"},
            {"value": "Closed Won", "label": "Closed Won"},
            {"value": "Closed Lost", "label": "Closed Lost"},
            {"value": "Custom Open", "label": "Custom Open"},
        ]
    }
    opportunity_stages = [
        {
            "MasterLabel": "Prospecting",
            "ApiName": "Prospecting",
            "SortOrder": 10,
            "DefaultProbability": 20,
            "IsClosed": False,
            "IsWon": False,
            "IsActive": True,
            "ForecastCategory": "Pipeline",
        },
        {
            "MasterLabel": "Closed Won",
            "ApiName": "Closed Won",
            "SortOrder": 99,
            "DefaultProbability": 100,
            "IsClosed": True,
            "IsWon": True,
            "IsActive": True,
            "ForecastCategory": "Closed",
        },
        {
            "MasterLabel": "Closed Lost",
            "ApiName": "Closed Lost",
            "SortOrder": 100,
            "DefaultProbability": 0,
            "IsClosed": True,
            "IsWon": False,
            "IsActive": True,
            "ForecastCategory": "Omitted",
        },
    ]

    pipelines, skipped_pipelines, skipped_stages, unresolved_stage_metadata = (
        normalize_salesforce_pipelines(
            record_types=record_types,
            stage_values_by_record_type=stage_values_by_record_type,
            opportunity_stages=opportunity_stages,
        )
    )

    assert skipped_pipelines == 0
    assert skipped_stages == 0
    assert unresolved_stage_metadata == 1
    assert len(pipelines) == 1
    pipeline = pipelines[0]
    assert pipeline.external_id == "012A"
    assert pipeline.name == "Enterprise_Sales"
    assert pipeline.label == "Enterprise Sales"
    assert [stage.external_id for stage in pipeline.stages] == [
        "012A:Prospecting",
        "012A:Closed Won",
        "012A:Closed Lost",
        "012A:Custom Open",
    ]
    assert [stage.stage_state for stage in pipeline.stages] == [
        CRMStageState.OPEN,
        CRMStageState.WON,
        CRMStageState.LOST,
        CRMStageState.OPEN,
    ]
    assert pipeline.stages[0].probability == 20.0
    assert pipeline.stages[1].forecast_category == "Closed"
    assert pipeline.stages[3].probability is None


def test_normalize_salesforce_pipelines_treats_only_zero_omitted_stage_as_lost() -> None:
    record_types = [
        {
            "Id": "012A",
            "Name": "Enterprise Sales",
            "DeveloperName": "Enterprise_Sales",
            "IsActive": True,
            "IsMaster": False,
        }
    ]
    stage_values_by_record_type = {
        "012A": [
            {"value": "No Decision", "label": "No Decision"},
            {"value": "On Hold", "label": "On Hold"},
        ]
    }
    opportunity_stages = [
        {
            "ApiName": "No Decision",
            "DefaultProbability": 0,
            "IsClosed": False,
            "IsWon": False,
            "IsActive": True,
            "ForecastCategory": "Omitted",
        },
        {
            "ApiName": "On Hold",
            "DefaultProbability": 0,
            "IsClosed": False,
            "IsWon": False,
            "IsActive": True,
            "ForecastCategory": "Pipeline",
        },
    ]

    pipelines, *_ = normalize_salesforce_pipelines(
        record_types=record_types,
        stage_values_by_record_type=stage_values_by_record_type,
        opportunity_stages=opportunity_stages,
    )

    assert [stage.stage_state for stage in pipelines[0].stages] == [
        CRMStageState.LOST,
        CRMStageState.OPEN,
    ]


def test_normalize_salesforce_pipelines_uses_master_record_type_fallback() -> None:
    record_types = [
        {
            "Id": "012MASTER",
            "Name": "Master",
            "DeveloperName": "Master",
            "Description": None,
            "IsActive": True,
            "IsMaster": True,
        }
    ]
    stage_values_by_record_type = {
        "012MASTER": [
            {"value": "Qualification", "label": "Qualification"},
        ]
    }
    opportunity_stages = [
        {
            "MasterLabel": "Qualification",
            "ApiName": "Qualification",
            "SortOrder": 5,
            "DefaultProbability": 10,
            "IsClosed": False,
            "IsWon": False,
            "IsActive": True,
            "ForecastCategory": "Pipeline",
        }
    ]

    pipelines, skipped_pipelines, skipped_stages, unresolved_stage_metadata = (
        normalize_salesforce_pipelines(
            record_types=record_types,
            stage_values_by_record_type=stage_values_by_record_type,
            opportunity_stages=opportunity_stages,
        )
    )

    assert skipped_pipelines == 0
    assert skipped_stages == 0
    assert unresolved_stage_metadata == 0
    assert len(pipelines) == 1
    assert pipelines[0].external_id == "master"
    assert pipelines[0].stages[0].external_id == "master:Qualification"


def test_normalize_salesforce_pipelines_keeps_master_with_custom_record_types() -> None:
    record_types = [
        {
            "Id": "012MASTER",
            "Name": "Master",
            "DeveloperName": "Master",
            "IsActive": True,
            "IsMaster": True,
        },
        {
            "Id": "012CUSTOM",
            "Name": "Enterprise Sales",
            "DeveloperName": "Enterprise_Sales",
            "IsActive": True,
            "IsMaster": False,
        },
    ]
    stage_values_by_record_type = {
        "012MASTER": [{"value": "Qualification", "label": "Qualification"}],
        "012CUSTOM": [{"value": "Discovery", "label": "Discovery"}],
    }

    pipelines, skipped_pipelines, skipped_stages, unresolved_stage_metadata = (
        normalize_salesforce_pipelines(
            record_types=record_types,
            stage_values_by_record_type=stage_values_by_record_type,
            opportunity_stages=[],
        )
    )

    assert skipped_pipelines == 0
    assert skipped_stages == 0
    assert unresolved_stage_metadata == 2
    assert [pipeline.external_id for pipeline in pipelines] == ["master", "012CUSTOM"]
    assert [stage.external_id for pipeline in pipelines for stage in pipeline.stages] == [
        "master:Qualification",
        "012CUSTOM:Discovery",
    ]
