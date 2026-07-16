"""Regression coverage for the one-off CCCI customer-ledger converter."""

import csv

from scripts.maintenance.convert_ccci_sales_report import OUTPUT_HEADERS, convert_csv


def test_convert_csv_keeps_notes_and_flags_an_unidentified_contact(tmp_path):
    """A phone without a real person's name must not create a fabricated contact."""
    source_path = tmp_path / "source.csv"
    output_path = tmp_path / "CCCI-final.csv"
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(
            source,
            fieldnames=[
                "Customer Name",
                "Customer Stage",
                "Salesperson",
                "Phone",
                "Address",
                "City",
                "State",
                "GSTIN / Tax ID",
                "Consolidated Comments",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Customer Name": "A J Enterprises",
                "Customer Stage": "Order Awaited",
                "Salesperson": "Pankaj Gupta",
                "Phone": "Unknown (Owner)\n+91 93105 28349",
                "Address": "Plot No. 97",
                "City": "Greater Noida",
                "State": "Uttar Pradesh (IN)",
                "GSTIN / Tax ID": "09ABWFA9140N1ZK",
                "Consolidated Comments": "First note\nSecond note",
            }
        )

    converted_count = convert_csv(source_path, output_path)

    with output_path.open(encoding="utf-8", newline="") as output:
        rows = list(csv.DictReader(output))
    assert converted_count == 1
    assert list(rows[0]) == OUTPUT_HEADERS
    assert rows[0]["Deal Name"] == "A J Enterprises - CCCI-0001"
    assert rows[0]["Account Name"] == "A J Enterprises"
    assert rows[0]["Deal Owner"] == ""
    assert rows[0]["Deal Stage"] == ""
    assert rows[0]["Source Salesperson"] == "Pankaj Gupta"
    assert rows[0]["Source Customer Stage"] == "Order Awaited"
    assert rows[0]["Deal Description"] == ""
    assert rows[0]["Deal Note"] == "First note\nSecond note"
    assert rows[0]["Source Contact Details"] == "Unknown (Owner)\n+91 93105 28349"
    assert rows[0]["GSTIN / Tax ID"] == "09ABWFA9140N1ZK"
    assert rows[0]["Account State"] == "Uttar Pradesh"
    assert rows[0]["Account Country"] == "India"
    assert rows[0]["Account Phone"] == "+919310528349"
    assert rows[0]["Contact Name"] == ""
    assert rows[0]["Contact Phone"] == ""
    assert rows[0]["Source Row"] == "2"
    assert rows[0]["Import Ready"] == "No"
    assert "Contact name required" in rows[0]["Review Issues"]
    assert "account-user email" in rows[0]["Review Issues"]
    assert "pipeline stage" in rows[0]["Review Issues"]
    assert rows[0]["Contact Job Title"] == ""
    assert "Legacy" not in "".join(rows[0].values())


def test_convert_csv_extracts_a_named_primary_contact(tmp_path):
    """A clear name, role, and phone become a contact attached to the row's account."""
    source_path = tmp_path / "source.csv"
    output_path = tmp_path / "CCCI-final.csv"
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(
            source,
            fieldnames=[
                "Customer Name",
                "Customer Stage",
                "Salesperson",
                "Phone",
                "Address",
                "City",
                "State",
                "GSTIN / Tax ID",
                "Consolidated Comments",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Customer Name": "A R Motors",
                "Customer Stage": "Convincing",
                "Salesperson": "Pankaj Gupta",
                "Phone": "Anil Garg (Owner) +91 98370 48504",
                "Address": "",
                "City": "Meerut",
                "State": "Uttar Pradesh (IN)",
                "GSTIN / Tax ID": "",
                "Consolidated Comments": "Discussed a sample order",
            }
        )

    convert_csv(source_path, output_path)

    with output_path.open(encoding="utf-8", newline="") as output:
        row = next(csv.DictReader(output))
    assert row["Account Phone"] == ""
    assert row["Contact Name"] == "Anil Garg"
    assert row["Contact Job Title"] == "Owner"
    assert row["Contact Phone"] == "+919837048504"
    assert "Contact name required" not in row["Review Issues"]


def test_convert_csv_marks_repeated_customer_names_for_deal_review(tmp_path):
    """Repeated customer rows require confirmation that two deals are intended."""
    source_path = tmp_path / "source.csv"
    output_path = tmp_path / "CCCI-final.csv"
    headers = [
        "Customer Name",
        "Customer Stage",
        "Salesperson",
        "Phone",
        "Address",
        "City",
        "State",
        "GSTIN / Tax ID",
        "Consolidated Comments",
    ]
    row = dict.fromkeys(headers, "")
    row.update({"Customer Name": "Speego", "Customer Stage": "Regular Supply"})
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=headers)
        writer.writeheader()
        writer.writerows([row, row])

    convert_csv(source_path, output_path)

    with output_path.open(encoding="utf-8", newline="") as output:
        rows = list(csv.DictReader(output))
    assert all("Duplicate account name" in row["Review Issues"] for row in rows)
