"""Copy a CRM import CSV while assigning every deal to one application user."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def retarget_deal_owner(
    source_path: Path,
    output_path: Path,
    owner_email: str,
) -> int:
    """Create a copy with every Deal Owner changed and all audit columns preserved."""
    if source_path.resolve() == output_path.resolve():
        raise ValueError("Source and output paths must be different")
    if owner_email.count("@") != 1:
        raise ValueError("Deal owner must be an email address")

    with source_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        headers = reader.fieldnames or []
        if "Deal Owner" not in headers:
            raise ValueError("Source CSV must contain a Deal Owner column")
        rows = list(reader)

    for row in rows:
        row["Deal Owner"] = owner_email

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    """Retarget a CRM import CSV from command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("owner_email")
    args = parser.parse_args()
    count = retarget_deal_owner(args.source, args.output, args.owner_email)
    print(f"Retargeted {count} rows to {args.owner_email} in {args.output}")


if __name__ == "__main__":
    main()
