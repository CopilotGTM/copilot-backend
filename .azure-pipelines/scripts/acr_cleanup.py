#!/usr/bin/env python3
"""Apply a conservative Azure Container Registry image-retention policy."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Delete old ACR image tags and old untagged manifests while protecting "
            "latest and the current deployment tag."
        )
    )
    parser.add_argument("--registry", required=True, help="ACR name, not login server.")
    parser.add_argument(
        "--repository",
        action="append",
        required=True,
        help="Repository to clean. Pass once per repository.",
    )
    parser.add_argument(
        "--keep-tags",
        type=int,
        default=20,
        help="Number of non-latest tags to retain per repository.",
    )
    parser.add_argument(
        "--untagged-days",
        type=int,
        default=7,
        help="Delete untagged manifests older than this many days.",
    )
    parser.add_argument(
        "--protect-tag",
        action="append",
        default=[],
        help="Tag that must never be deleted. Pass once per protected tag.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned deletes without deleting anything.",
    )
    return parser.parse_args()


def azure_cli() -> str:
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI executable 'az' was not found in PATH.")
    return executable


def normalize_registry_name(value: str) -> str:
    registry = value.strip()
    if registry.endswith(".azurecr.io"):
        registry = registry.split(".", 1)[0]

    if not re.fullmatch(r"[A-Za-z0-9]{5,50}", registry):
        raise ValueError(
            "--registry must be an ACR name or login server, not an unresolved pipeline macro. "
            f"Received: {value!r}"
        )
    return registry


def run_json(command: list[str]) -> object:
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if not completed.stdout.strip():
        return []
    return json.loads(completed.stdout)


def run_delete(command: list[str], dry_run: bool) -> None:
    printable = " ".join(command)
    if dry_run:
        print(f"DRY RUN: {printable}")
        return

    print(f"DELETE: {printable}")
    subprocess.run(command, check=False)


def parse_azure_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def list_tags(registry: str, repository: str) -> list[str]:
    tags = run_json(
        [
            azure_cli(),
            "acr",
            "repository",
            "show-tags",
            "--name",
            registry,
            "--repository",
            repository,
            "--orderby",
            "time_desc",
            "--output",
            "json",
        ]
    )
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags if tag]


def list_manifests(registry: str, repository: str) -> list[dict[str, object]]:
    manifests = run_json(
        [
            azure_cli(),
            "acr",
            "manifest",
            "list-metadata",
            "--registry",
            registry,
            "--name",
            repository,
            "--orderby",
            "time_desc",
            "--output",
            "json",
        ]
    )
    if not isinstance(manifests, list):
        return []
    return [item for item in manifests if isinstance(item, dict)]


def cleanup_tags(
    registry: str,
    repository: str,
    keep_tags: int,
    protected_tags: set[str],
    dry_run: bool,
) -> int:
    tags = [
        tag
        for tag in list_tags(registry, repository)
        if tag != "latest" and tag not in protected_tags
    ]
    old_tags = tags[keep_tags:]

    print(
        f"{repository}: {len(tags)} cleanup-eligible tags, "
        f"keeping {min(len(tags), keep_tags)}, deleting {len(old_tags)}"
    )
    for tag in old_tags:
        run_delete(
            [
                azure_cli(),
                "acr",
                "repository",
                "untag",
                "--name",
                registry,
                "--image",
                f"{repository}:{tag}",
            ],
            dry_run,
        )
    return len(old_tags)


def cleanup_untagged_manifests(
    registry: str,
    repository: str,
    cutoff: datetime,
    dry_run: bool,
) -> int:
    delete_count = 0
    for manifest in list_manifests(registry, repository):
        tags = manifest.get("tags")
        if tags:
            continue

        digest = manifest.get("digest")
        last_updated = parse_azure_time(str(manifest.get("lastUpdateTime", "")))
        if not digest or last_updated is None or last_updated >= cutoff:
            continue

        delete_count += 1
        run_delete(
            [
                azure_cli(),
                "acr",
                "manifest",
                "delete",
                "--registry",
                registry,
                "--name",
                f"{repository}@{digest}",
                "--yes",
            ],
            dry_run,
        )

    print(f"{repository}: deleting {delete_count} untagged manifests older than {cutoff.date()}")
    return delete_count


def main() -> int:
    args = parse_args()
    if args.keep_tags < 1:
        print("--keep-tags must be at least 1", file=sys.stderr)
        return 2
    if args.untagged_days < 1:
        print("--untagged-days must be at least 1", file=sys.stderr)
        return 2

    try:
        registry = normalize_registry_name(args.registry)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    protected_tags = {tag for tag in args.protect_tag if tag}
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.untagged_days)

    print(
        "ACR cleanup policy: "
        f"registry={registry}, keep_tags={args.keep_tags}, "
        f"untagged_days={args.untagged_days}, dry_run={args.dry_run}"
    )
    if protected_tags:
        print(f"Protected tags: {', '.join(sorted(protected_tags))}")

    deleted_tags = 0
    deleted_manifests = 0
    failures = 0
    for repository in args.repository:
        print("")
        print(f"Processing repository: {repository}")
        try:
            deleted_tags += cleanup_tags(
                registry,
                repository,
                args.keep_tags,
                protected_tags,
                args.dry_run,
            )
            deleted_manifests += cleanup_untagged_manifests(
                registry,
                repository,
                cutoff,
                args.dry_run,
            )
        except subprocess.CalledProcessError as exc:
            failures += 1
            print(f"Failed to clean {repository}: {exc}", file=sys.stderr)
            if exc.stderr:
                print(exc.stderr.strip(), file=sys.stderr)

    print("")
    print(f"Cleanup complete. Tags: {deleted_tags}, untagged manifests: {deleted_manifests}")
    if failures:
        print(f"Cleanup completed with {failures} repository failure(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
