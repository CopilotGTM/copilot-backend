from src.service.crm.imports.csv_parser import (
    MAX_CSV_COLUMNS,
    MAX_CSV_FILE_SIZE_BYTES,
    MAX_CSV_ROWS,
    parse_crm_csv_content,
    sanitize_csv_cell_for_export,
)


def test_parse_csv_handles_utf8_bom_blank_rows_and_duplicate_headers() -> None:
    csv_bytes = (
        "\ufeffEmail,Company,Email\n"
        "jane@example.com,Acme,jane.alt@example.com\n"
        "   ,   ,   \n"
        "john@example.com,Beta,john.alt@example.com\n"
    ).encode()

    result = parse_crm_csv_content(csv_bytes)

    assert result.blocking_errors == []
    assert result.headers == ["Email", "Company", "Email (2)"]
    assert result.total_rows == 2
    assert result.sample_rows == [
        {
            "Email": "jane@example.com",
            "Company": "Acme",
            "Email (2)": "jane.alt@example.com",
        },
        {
            "Email": "john@example.com",
            "Company": "Beta",
            "Email (2)": "john.alt@example.com",
        },
    ]


def test_parse_csv_reports_invalid_csv() -> None:
    result = parse_crm_csv_content('Email,Company\n"jane@example.com,Acme\n')

    assert [error.code for error in result.blocking_errors] == ["invalid_csv"]
    assert result.headers == []
    assert result.total_rows == 0


def test_parse_csv_reports_invalid_utf8_bytes() -> None:
    result = parse_crm_csv_content(b"\xff\xfe\x00")

    assert [error.code for error in result.blocking_errors] == ["invalid_csv"]


def test_parse_csv_reports_file_size_row_and_column_caps() -> None:
    too_large = b"a" * (MAX_CSV_FILE_SIZE_BYTES + 1)
    too_many_columns = ",".join(f"col{i}" for i in range(MAX_CSV_COLUMNS + 1))
    too_many_rows = "Email\n" + "\n".join(f"user{i}@example.com" for i in range(MAX_CSV_ROWS + 1))

    assert [error.code for error in parse_crm_csv_content(too_large).blocking_errors] == [
        "file_too_large"
    ]
    assert [error.code for error in parse_crm_csv_content(too_many_columns).blocking_errors] == [
        "too_many_columns"
    ]
    assert [error.code for error in parse_crm_csv_content(too_many_rows).blocking_errors] == [
        "too_many_rows"
    ]


def test_formula_looking_values_are_preserved_for_preview_and_sanitized_for_export() -> None:
    result = parse_crm_csv_content('Email,Note\njane@example.com,"=HYPERLINK(""bad"")"\n')

    assert result.sample_rows[0]["Note"] == '=HYPERLINK("bad")'
    assert sanitize_csv_cell_for_export("=1+1") == "'=1+1"
    assert sanitize_csv_cell_for_export("+1") == "'+1"
    assert sanitize_csv_cell_for_export("-1") == "'-1"
    assert sanitize_csv_cell_for_export("@cmd") == "'@cmd"
    assert sanitize_csv_cell_for_export("safe") == "safe"
    assert sanitize_csv_cell_for_export(None) == ""
