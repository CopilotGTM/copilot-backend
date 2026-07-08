from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

# copilotgtm: V1 keeps CSV imports eager and capped; stream Azure blob chunks
# into resumable row batches if customers need larger imports.
MAX_CSV_FILE_SIZE_BYTES = 10 * 1024 * 1024
MAX_CSV_ROWS = 25_000
MAX_CSV_COLUMNS = 200
DEFAULT_SAMPLE_SIZE = 20


@dataclass(frozen=True)
class CSVParseError:
    code: str
    message: str


@dataclass(frozen=True)
class CSVParseResult:
    headers: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, str]] = field(default_factory=list)
    total_rows: int = 0
    blocking_errors: list[CSVParseError] = field(default_factory=list)


def sanitize_csv_cell_for_export(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def parse_crm_csv_content(
    content: bytes | str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> CSVParseResult:
    if isinstance(content, bytes):
        if len(content) > MAX_CSV_FILE_SIZE_BYTES:
            return _blocking("file_too_large", "CSV file exceeds the 10 MB limit")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return _blocking("invalid_csv", f"Invalid CSV: {exc}")
    else:
        if len(content.encode("utf-8")) > MAX_CSV_FILE_SIZE_BYTES:
            return _blocking("file_too_large", "CSV file exceeds the 10 MB limit")
        text = content.lstrip("\ufeff")

    try:
        stream = io.StringIO(text)
        header_reader = csv.reader(stream, strict=True)
        raw_headers = next(header_reader, None)
        if raw_headers is None:
            return _blocking("missing_header", "CSV must include a header row")
        headers = _normalize_headers(raw_headers)
        if len(headers) > MAX_CSV_COLUMNS:
            return _blocking("too_many_columns", "CSV exceeds the 200 column limit")
        if not any(header.strip() for header in headers):
            return _blocking("missing_header", "CSV must include a non-empty header row")

        sample_rows: list[dict[str, str]] = []
        total_rows = 0
        reader = csv.DictReader(
            stream,
            fieldnames=headers,
            restkey="_extra",
            restval="",
            strict=True,
        )
        for raw_row in reader:
            values = [raw_row.get(header) or "" for header in headers]
            if _is_blank_row(values):
                continue
            total_rows += 1
            if total_rows > MAX_CSV_ROWS:
                return _blocking("too_many_rows", "CSV exceeds the 25,000 row limit")
            if len(sample_rows) < sample_size:
                sample_rows.append({header: raw_row.get(header) or "" for header in headers})
    except csv.Error as exc:
        return _blocking("invalid_csv", f"Invalid CSV: {exc}")

    return CSVParseResult(headers=headers, sample_rows=sample_rows, total_rows=total_rows)


def _blocking(code: str, message: str) -> CSVParseResult:
    return CSVParseResult(blocking_errors=[CSVParseError(code=code, message=message)])


def _normalize_headers(headers: list[str]) -> list[str]:
    normalized: list[str] = []
    counts: dict[str, int] = {}
    for index, header in enumerate(headers, start=1):
        base = header.strip() or f"Column {index}"
        count = counts.get(base, 0) + 1
        counts[base] = count
        normalized.append(base if count == 1 else f"{base} ({count})")
    return normalized


def _is_blank_row(values: list[str]) -> bool:
    return all(not value.strip() for value in values)
