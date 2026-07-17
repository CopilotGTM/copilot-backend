"""Regression coverage for the one-off CCCI customer-ledger converter."""

import csv

from scripts.maintenance.convert_ccci_sales_report import OUTPUT_HEADERS, convert_csv


def test_convert_csv_keeps_notes_and_skips_an_unidentified_contact(tmp_path):
    """A missing contact must not block an otherwise valid account and deal."""
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
    assert rows[0]["Deal Owner"] == "pankajgupta101@gmail.com"
    assert rows[0]["Deal Stage"] == "Order Awaited"
    assert rows[0]["Source Salesperson"] == "Pankaj Gupta"
    assert rows[0]["Source Customer Stage"] == "Order Awaited"
    assert rows[0]["Deal Description"] == ""
    assert rows[0]["Deal Note"] == "First note\nSecond note"
    assert rows[0]["Source Contact Details"] == "Unknown (Owner)\n+91 93105 28349"
    assert rows[0]["GSTIN / Tax ID"] == "09ABWFA9140N1ZK"
    assert rows[0]["Source GSTIN / Tax ID"] == "09ABWFA9140N1ZK"
    assert rows[0]["GSTIN Validation"] == "Valid"
    assert rows[0]["Account State"] == "Uttar Pradesh"
    assert rows[0]["Account Country"] == "India"
    assert rows[0]["Account Phone"] == "+919310528349"
    assert rows[0]["Contact Name"] == ""
    assert rows[0]["Contact Phone"] == ""
    assert rows[0]["Source Row"] == "2"
    assert rows[0]["Import Ready"] == "Yes"
    assert "No contact will be created" in rows[0]["Review Issues"]
    assert "defaulted to Toufiq Hussain" not in rows[0]["Review Issues"]
    assert "account-user email" not in rows[0]["Review Issues"]
    assert "pipeline stage" not in rows[0]["Review Issues"]
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
    assert row["Deal Owner"] == "pankajgupta101@gmail.com"
    assert row["Deal Stage"] == "Convincing"
    assert row["Import Ready"] == "Yes"
    assert "Contact name required" not in row["Review Issues"]


def test_all_customer_stages_and_known_org_users_map_to_live_values(tmp_path):
    """Production stages and exact Manish/Himanshu identities populate import fields."""
    source_path = tmp_path / "source.csv"
    output_path = tmp_path / "CCCI-final.csv"
    stages = [
        "Convincing",
        "Sample payment pending stage",
        "Order Awaited",
        "Regular Supply",
        "Payment recovery and dead forever",
        "Dead",
    ]
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
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=headers)
        writer.writeheader()
        for row_number, stage in enumerate(stages, start=1):
            row = dict.fromkeys(headers, "")
            row.update(
                {
                    "Customer Name": f"Account {row_number}",
                    "Customer Stage": stage,
                    "Salesperson": "Manish Tiwari" if row_number % 2 else "Himanshu Sahu",
                }
            )
            writer.writerow(row)

    convert_csv(source_path, output_path)

    with output_path.open(encoding="utf-8", newline="") as output:
        rows = list(csv.DictReader(output))
    assert [row["Deal Stage"] for row in rows] == stages
    assert {row["Deal Owner"] for row in rows} == {
        "manishkumartiwari10112000@gmail.com",
        "himanshu.sahu@voltworks.in",
    }
    assert all("defaulted to Toufiq Hussain" not in row["Review Issues"] for row in rows)


def test_convert_csv_preserves_manually_reviewed_secondary_contacts(tmp_path):
    """A manually reviewed multi-person cell keeps its primary and secondary contacts."""
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
    rows = []
    for row_number in range(1, 5):
        row = dict.fromkeys(headers, "")
        row.update(
            {
                "Customer Name": f"Account {row_number}",
                "Customer Stage": "Convincing",
                "Salesperson": "Pankaj Gupta",
            }
        )
        rows.append(row)
    rows[3]["Customer Name"] = "Aero Drive Private Limited"
    rows[3]["Phone"] = (
        "Kumar Avishek (Owner) +91 98702 93710 Rajesh (Overall Manager) +91 88267 47673"
    )
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    convert_csv(source_path, output_path)

    with output_path.open(encoding="utf-8", newline="") as output:
        row = list(csv.DictReader(output))[3]
    assert row["Contact Name"] == "Kumar Avishek"
    assert row["Contact Job Title"] == "Owner"
    assert row["Contact Phone"] == "+919870293710"
    assert row["Additional Contact Details"] == ("Rajesh (Overall Manager): +918826747673")


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
    row.update(
        {
            "Customer Name": "Speego",
            "Customer Stage": "Regular Supply",
            "GSTIN / Tax ID": "Yes",
        }
    )
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=headers)
        writer.writeheader()
        writer.writerows([row, row])

    convert_csv(source_path, output_path)

    with output_path.open(encoding="utf-8", newline="") as output:
        rows = list(csv.DictReader(output))
    assert all("Duplicate account name" in row["Review Issues"] for row in rows)
    assert {row["Deal Owner"] for row in rows} == {"toufiq.hussain@voltworks.in"}
    assert all("salesperson is blank" in row["Review Issues"] for row in rows)
    assert all(row["GSTIN / Tax ID"] == "" for row in rows)
    assert all(row["Source GSTIN / Tax ID"] == "Yes" for row in rows)
    assert all(row["GSTIN Validation"] == "Invalid" for row in rows)


def test_convert_csv_keeps_contacts_found_during_manual_note_review(tmp_path):
    """Names explicitly identified in notes become contacts without inventing details."""
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
    reviewed_contacts = [
        ("A V Enterprises", "+91 73046 39132", "Ashok", ""),
        ("A.V. AUTOMOTIVES", "", "Neeraj Ji", "Plant Head"),
        ("CELLETRON MOTORS PVT L.T.D", "+91 98183 43591", "Sartak", ""),
        ("BALAJI TRADING", "+91 70075 13072", "Prem", ""),
        ("E-MERAG", "+91 80 4860 5852", "Itti", "Son of Owner"),
        ("G.T TRADER", "+91 88003 09098", "Ajit", ""),
        ("RCJ AUTO FORGE PRIVATE LIMITED", "", "Katiyar", "Purchase"),
        ("KHANDELWAL ENTERPRISES", "+91 95999 02751", "Veer Bharat", ""),
    ]
    with source_path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=headers)
        writer.writeheader()
        for account, phone, _, _ in reviewed_contacts:
            row = dict.fromkeys(headers, "")
            row.update(
                {
                    "Customer Name": account,
                    "Customer Stage": "Convincing",
                    "Salesperson": "Pankaj Gupta",
                    "Phone": phone,
                }
            )
            writer.writerow(row)

    convert_csv(source_path, output_path)

    with output_path.open(encoding="utf-8", newline="") as output:
        rows = list(csv.DictReader(output))
    assert [(row["Contact Name"], row["Contact Job Title"]) for row in rows] == [
        (name, role) for _, _, name, role in reviewed_contacts
    ]
    assert all(row["Import Ready"] == "Yes" for row in rows)
