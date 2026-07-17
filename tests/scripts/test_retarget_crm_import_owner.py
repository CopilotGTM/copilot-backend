"""Regression coverage for retargeting CRM import rows to one application user."""

import csv

from scripts.maintenance.retarget_crm_import_owner import retarget_deal_owner


def test_retarget_deal_owner_changes_only_the_import_owner(tmp_path):
    """Every deal points to the target user while source salesperson audit data survives."""
    source_path = tmp_path / "CCCI-final.csv"
    output_path = tmp_path / "CCCI-dev-final.csv"
    headers = ["Deal Name", "Deal Owner", "Source Salesperson"]
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=headers)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "Deal Name": "Deal 1",
                    "Deal Owner": "pankaj@example.com",
                    "Source Salesperson": "Pankaj Gupta",
                },
                {
                    "Deal Name": "Deal 2",
                    "Deal Owner": "toufiq@example.com",
                    "Source Salesperson": "Jagmohan Singh",
                },
            ]
        )

    count = retarget_deal_owner(
        source_path,
        output_path,
        "nikhilsinghal005@gmail.com",
    )

    with output_path.open(encoding="utf-8", newline="") as output:
        rows = list(csv.DictReader(output))
    assert count == 2
    assert {row["Deal Owner"] for row in rows} == {"nikhilsinghal005@gmail.com"}
    assert [row["Source Salesperson"] for row in rows] == [
        "Pankaj Gupta",
        "Jagmohan Singh",
    ]
