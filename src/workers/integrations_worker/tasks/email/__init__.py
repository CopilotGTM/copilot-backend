"""Active email sync task exports.

Email sync now runs from event/post-connect integration dispatch. The old
all-org and org fanout tasks live under `tasks.deprecated.email`.
"""

from .integration_task import sync_email_integration_task

__all__ = ["sync_email_integration_task"]
