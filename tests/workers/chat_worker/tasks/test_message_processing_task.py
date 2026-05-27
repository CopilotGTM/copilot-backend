from __future__ import annotations

from uuid import UUID, uuid4

from src.workers.chat_worker.tasks.message_processing import process_message


def test_process_message_coerces_celery_string_ids(mocker):
    """Celery payloads are JSON strings; task boundary must restore UUIDs."""
    org_id = uuid4()
    job_id = uuid4()
    service_cls = mocker.patch(
        "src.workers.chat_worker.tasks.message_processing.MessageProcessingService"
    )
    service_cls.return_value.process.return_value = True

    process_message.run(str(org_id), str(job_id))

    service_cls.assert_called_once_with(org_id)
    service_cls.return_value.process.assert_called_once()
    actual_job_id = service_cls.return_value.process.call_args.args[0]
    assert actual_job_id == job_id
    assert isinstance(actual_job_id, UUID)
