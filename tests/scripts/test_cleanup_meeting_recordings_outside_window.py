from __future__ import annotations

from uuid import uuid4

from scripts.cleanup_meeting_recordings_outside_window import (
    build_parser,
    should_delete_after_recall_cancel,
)


def test_cleanup_script_defaults_to_dry_run() -> None:
    """The Exatel cleanup script must require an explicit apply flag."""
    args = build_parser().parse_args(["--organisation-id", str(uuid4())])

    assert args.apply is False


def test_cleanup_script_deletes_locally_only_after_recall_is_safe() -> None:
    """Local rows are removed only when Recall cancellation cannot leave an active bot."""
    assert should_delete_after_recall_cancel("cancelled") is True
    assert should_delete_after_recall_cancel("already_missing") is True
    assert should_delete_after_recall_cancel("failed") is False
