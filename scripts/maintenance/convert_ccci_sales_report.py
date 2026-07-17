"""Convert the CCCI customer ledger into a reviewable CRM deal-import CSV.

The CRM import creates an account, a deal, a note, and an optional contact from
one row. This converter extracts only a defensible primary contact and marks
every remaining mapping or data-quality decision for human review.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path

SOURCE_HEADERS = [
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

OUTPUT_HEADERS = [
    "Deal Name",
    "Account Name",
    "Deal Stage",
    "Deal Owner",
    "Deal Description",
    "Deal Note",
    "Account Phone",
    "Account Address",
    "Account City",
    "Account State",
    "Account Country",
    "Contact Name",
    "Contact Job Title",
    "Contact Phone",
    "Contact Email",
    "Additional Contact Details",
    "Source Customer Stage",
    "Source Salesperson",
    "Source Contact Details",
    "GSTIN / Tax ID",
    "Source GSTIN / Tax ID",
    "GSTIN Validation",
    "Source Row",
    "Import Ready",
    "Review Issues",
]

_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?91[\s-]*)?(?:\d[\s-]*){9}\d(?!\d)")
_ROLE_PATTERN = re.compile(r"\((?P<role>[^()]*)\)\s*$")
_HONORIFIC_PATTERN = re.compile(r"^(?:mr|mrs|ms|miss|dr)\.?\s+", re.IGNORECASE)
_VALID_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z .&'-]{1,79}")
_INVALID_CONTACT_NAMES = {"unknown", "not known", "owner", "phone", "contact"}
_GSTIN_PATTERN = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]")

# copilotgtm: This is a one-off client migration, so manually reviewed ambiguous
# contacts are keyed by account name. Replace this with a multi-contact import
# shape if the generic importer later supports several contacts per deal row.
_MANUAL_CONTACT_OVERRIDES = {
    "a v enterprises": ("Ashok", "", "", ""),
    "a.v. automotives": ("Neeraj Ji", "Plant Head", "", ""),
    "aero drive private limited": (
        "Kumar Avishek",
        "Owner",
        "+919870293710",
        "Rajesh (Overall Manager): +918826747673",
    ),
    "hindustan motor corporation": (
        "Saif Ali Khan",
        "Owner",
        "+918952953786",
        "Lokendra (Production Purchase): +917340550035",
    ),
    "celletron motors pvt l.t.d": ("Sartak", "", "", ""),
    "balaji trading": ("Prem", "", "", ""),
    "e-merag": ("Itti", "Son of Owner", "", ""),
    "g.t trader": ("Ajit", "", "+918800309098", ""),
    "rcj auto forge private limited": ("Katiyar", "Purchase", "", ""),
    "khandelwal enterprises": ("Veer Bharat", "", "", ""),
    "om balajee automobiles": ("Prashant Tyagi", "", "", ""),
    "platinum mahindra": (
        "Simar",
        "Owner",
        "+917814862062",
        "Sanjeev: +919257400555",
    ),
    "kabmax electric automobiles private limited": (
        "Ajay Ji",
        "Owner",
        "+919410685255",
        "Brij Bhusan (Jijaji of Owner): +917942696863",
    ),
    "sathi motors electric vehicle private limited": (
        "Lokendra",
        "",
        "+919871907934",
        "",
    ),
    "shakti auto green": (
        "Krishan Kumar",
        "Owner",
        "+919899411001",
        "Harshdeep (Owner Son): +918569894062",
    ),
    "shakti engineering works": (
        "",
        "",
        "+917300093027",
        "Additional phone: +916350317360",
    ),
    "shri radhey govind automobile": (
        "Raj Chabra",
        "",
        "+919988711448",
        "Additional phone: +916239343881",
    ),
    "pari metro e rickshaw": (
        "Veerendra Ji",
        "Owner",
        "+919837488704",
        "Additional phone: +919548124914",
    ),
    "rakheja industries": (
        "Kuljeet Singh",
        "",
        "+919999038817",
        "Padhi Ji: +919711627636",
    ),
    "shri-shyam e-rickshaw": (
        "Pramod Jindal",
        "Main",
        "+919313312625",
        "Praveen Jindal: +919210669225",
    ),
    "sun industries": (
        "Sanjay Kumar",
        "Purchase",
        "+919084654617",
        "Vikash Ji (Plant Head): +918076537866",
    ),
    "supertech l3 ev limited": (
        "Yateem Sharma",
        "Owner",
        "+918822746655",
        "Additional phone: +919138363630",
    ),
    "supertech l5 ev limited": (
        "Yateem Sharma",
        "Owner",
        "+918822746655",
        "Additional phone: +919138363630",
    ),
    "victory electric vehicles international l.t.d": (
        "Bunty",
        "Purchase",
        "+919218057418",
        "Vinod (Manufacturing): +919991014335",
    ),
    "shyam traders": (
        "",
        "",
        "+918090382177",
        "Additional phone: +919161906730",
    ),
    "sona tempo house": (
        "",
        "",
        "+918090259839",
        "Additional phones: +916307384213; +919452283222",
    ),
    "shree mahatma auto": (
        "",
        "",
        "+919125186049",
        "Additional phone: +919125186069",
    ),
    "city shine": (
        "",
        "",
        "+918572000587",
        "Additional phone: +919255147587",
    ),
}

# These labels and owners were read from Voltworks production on 2026-07-17.
# Keep the source columns in the output so a later production change is auditable.
_LIVE_STAGE_LABELS = {
    label.casefold(): label
    for label in (
        "Convincing",
        "Sample payment pending stage",
        "Order Awaited",
        "Regular Supply",
        "Payment recovery and dead forever",
        "Dead",
    )
}
_KNOWN_OWNER_EMAILS = {
    "manish tiwari": "manishkumartiwari10112000@gmail.com",
    "pankaj gupta": "pankajgupta101@gmail.com",
    "himanshu sahu": "himanshu.sahu@voltworks.in",
    "toufiq hussain": "toufiq.hussain@voltworks.in",
}
_TOUFIQ_EMAIL = _KNOWN_OWNER_EMAILS["toufiq hussain"]


def convert_csv(source_path: Path, output_path: Path) -> int:
    """Convert source customers into import rows plus explicit review guidance."""
    with source_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing_headers = sorted(set(SOURCE_HEADERS) - set(reader.fieldnames or []))
        if missing_headers:
            raise ValueError(f"Missing required source columns: {', '.join(missing_headers)}")
        rows = list(reader)

    account_counts = Counter(
        row["Customer Name"].strip().casefold() for row in rows if row["Customer Name"].strip()
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=OUTPUT_HEADERS)
        writer.writeheader()
        for row_number, row in enumerate(rows, start=1):
            account_key = row["Customer Name"].strip().casefold()
            writer.writerow(
                _convert_row(
                    row,
                    row_number,
                    duplicate_account=bool(account_key and account_counts[account_key] > 1),
                )
            )
    return len(rows)


def _convert_row(
    row: dict[str, str],
    row_number: int,
    *,
    duplicate_account: bool,
) -> dict[str, str]:
    """Build one import row without inventing owners, stages, or contact names."""
    customer_name = row["Customer Name"].strip()
    source_state = row["State"].strip()
    source_contact = row["Phone"].strip()
    source_owner = row["Salesperson"].strip()
    source_stage = row["Customer Stage"].strip()
    source_tax_id = row["GSTIN / Tax ID"].strip()
    normalized_tax_id = source_tax_id.upper()
    tax_id = normalized_tax_id if _GSTIN_PATTERN.fullmatch(normalized_tax_id) else ""
    tax_validation = "Valid" if tax_id else ("Invalid" if source_tax_id else "")
    deal_stage = _map_deal_stage(source_stage)
    deal_owner, owner_mapping_note = _map_deal_owner(source_owner)
    manual_contact = _MANUAL_CONTACT_OVERRIDES.get(customer_name.casefold())
    if manual_contact:
        contact_name, contact_role, contact_phone, additional_contacts = manual_contact
        multiple_contacts = bool(additional_contacts)
    else:
        contact_name, contact_role, contact_phone, multiple_contacts = _extract_primary_contact(
            source_contact
        )
        additional_contacts = ""

    review_issues = [owner_mapping_note] if owner_mapping_note else []
    if not deal_stage:
        review_issues.append(f"Customer stage is not mapped: {source_stage or '(blank)'}")
    if not customer_name:
        review_issues.append("Account name required")
    if not contact_name:
        review_issues.append("No contact will be created; contact name unavailable")
    elif not contact_phone:
        review_issues.append("Contact phone missing")
    if additional_contacts:
        review_issues.append("Additional contacts/phones preserved for separate import")
    elif multiple_contacts:
        review_issues.append("Multiple contacts found; confirm the primary contact")
    if duplicate_account:
        review_issues.append("Duplicate account name; confirm a second deal is intended")
    if tax_id:
        review_issues.append("Map GSTIN / Tax ID to a custom field or ignore the column")
    elif source_tax_id:
        review_issues.append("Invalid GSTIN source value; validated GSTIN left blank")

    # A phone without a defensible person's name remains useful as the account's
    # reachable number, but it must not create a fabricated CRM contact.
    source_phone_match = _PHONE_PATTERN.search(source_contact)
    source_phone = _normalize_phone(source_phone_match.group()) if source_phone_match else ""
    account_phone = source_phone if not contact_name or not contact_phone else ""

    return {
        "Deal Name": f"{customer_name} - CCCI-{row_number:04d}",
        "Account Name": customer_name,
        "Deal Stage": deal_stage,
        "Deal Owner": deal_owner,
        "Deal Description": "",
        "Deal Note": row["Consolidated Comments"].strip(),
        "Account Phone": account_phone,
        "Account Address": row["Address"].strip(),
        "Account City": row["City"].strip(),
        "Account State": source_state.removesuffix(" (IN)"),
        "Account Country": "India" if source_state.endswith(" (IN)") else "",
        "Contact Name": contact_name,
        "Contact Job Title": contact_role,
        "Contact Phone": contact_phone if contact_name else "",
        "Contact Email": "",
        "Additional Contact Details": additional_contacts,
        "Source Customer Stage": source_stage,
        "Source Salesperson": source_owner,
        "Source Contact Details": source_contact,
        "GSTIN / Tax ID": tax_id,
        "Source GSTIN / Tax ID": source_tax_id,
        "GSTIN Validation": tax_validation,
        "Source Row": str(row_number + 1),
        "Import Ready": ("Yes" if customer_name and deal_stage and deal_owner else "No"),
        "Review Issues": "; ".join(review_issues),
    }


def _map_deal_stage(source_stage: str) -> str:
    """Map a source customer stage to the matching live Voltworks stage label."""
    return _LIVE_STAGE_LABELS.get(source_stage.strip().casefold(), "")


def _map_deal_owner(source_owner: str) -> tuple[str, str]:
    """Preserve known org users and use Toufiq only for unresolvable legacy owners."""
    normalized_owner = source_owner.strip().casefold()
    if normalized_owner in _KNOWN_OWNER_EMAILS:
        return _KNOWN_OWNER_EMAILS[normalized_owner], ""
    if source_owner:
        return (
            _TOUFIQ_EMAIL,
            f"Owner defaulted to Toufiq Hussain from legacy salesperson {source_owner}",
        )
    return _TOUFIQ_EMAIL, "Owner defaulted to Toufiq Hussain because salesperson is blank"


def _extract_primary_contact(source_contact: str) -> tuple[str, str, str, bool]:
    """Extract the first named Indian contact while preserving ambiguity for review."""
    phone_matches = list(_PHONE_PATTERN.finditer(source_contact))
    phone = _normalize_phone(phone_matches[0].group()) if phone_matches else ""
    name_source = (
        source_contact[: phone_matches[0].start()].strip(" \t\r\n-:;,")
        if phone_matches
        else source_contact.strip()
    )

    # copilotgtm: A name-only cell can still create an unverified local contact.
    # Text containing an incomplete number is rejected until a stronger parser exists.
    if not phone_matches and any(character.isdigit() for character in name_source):
        return "", "", "", False

    role_match = _ROLE_PATTERN.search(name_source)
    role = role_match.group("role").strip() if role_match else ""
    if role_match:
        name_source = name_source[: role_match.start()].strip(" \t\r\n-:;,")

    name = " ".join(_HONORIFIC_PATTERN.sub("", name_source).split())
    if name.casefold() in _INVALID_CONTACT_NAMES or not _VALID_NAME_PATTERN.fullmatch(name):
        name = ""

    return name, role if name else "", phone, len(phone_matches) > 1


def _normalize_phone(phone: str) -> str:
    """Normalize a ten-digit Indian phone to an E.164-shaped value."""
    digits = "".join(character for character in phone if character.isdigit())
    local_digits = digits[2:] if len(digits) == 12 and digits.startswith("91") else digits
    return f"+91{local_digits}" if len(local_digits) == 10 else f"+{digits}"


def main() -> None:
    """Run the converter for paths supplied on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Path to the original CCCI CSV")
    parser.add_argument("output", type=Path, help="Path for the converted CSV")
    args = parser.parse_args()
    count = convert_csv(args.source, args.output)
    print(f"Converted {count} rows to {args.output}")


if __name__ == "__main__":
    main()
