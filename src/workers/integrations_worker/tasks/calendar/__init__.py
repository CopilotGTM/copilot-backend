"""Active calendar sync task exports.

Calendar sync now runs from event/post-connect integration dispatch. The old
all-org and org fanout tasks live under `tasks.deprecated.calendar`.
"""

from .integration_task import sync_calendar_integration_task

__all__ = ["sync_calendar_integration_task"]
