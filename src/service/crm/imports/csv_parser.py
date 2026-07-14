"""Bounded CSV parsing and spreadsheet-safe result export helpers."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field

# copilotgtm: V1 keeps the blob itself eager and capped at 10 MB; switch Azure
# download and parsing to chunk streams together if customers need larger files.
MAX_CSV_FILE_SIZE_BYTES = 10 * 1024 * 1024
MAX_CSV_ROWS = 25_000
MAX_CSV_COLUMNS = 200
MAX_CSV_HEADER_LENGTH = 512
DEFAULT_SAMPLE_SIZE = 20


@dataclass(frozen=True)
class CSVParseError:
    """A stable blocking parse error returned to the import API."""

    code: str
    message: str


@dataclass(frozen=True)
class CSVParseResult:
    """Headers, a bounded mapping sample, row count, and blocking errors."""

    headers: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, str]] = field(default_factory=list)
    total_rows: int = 0
    blocking_errors: list[CSVParseError] = field(default_factory=list)


def iter_crm_csv_rows(
    content: bytes | str,
    headers: list[str],
) -> Iterator[dict[str, str]]:
    """Yield validated non-blank rows sparsely to bound analysis memory."""
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff")
    stream = io.StringIO(text)
    next(csv.reader(stream, strict=True), None)
    reader = csv.DictReader(stream, fieldnames=headers, restkey=None, restval="", strict=True)
    for raw_row in reader:
        values = {header: raw_row.get(header) or "" for header in headers}
        if not _is_blank_row(list(values.values())):
            yield {header: value for header, value in values.items() if value != ""}


def sanitize_csv_cell_for_export(value: object) -> str:
    """Prevent imported values from becoming formulas in downloaded results."""

    if value is None:
        return ""
    text = str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def parse_crm_csv_content(
    content: bytes | str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> CSVParseResult:
    """Validate a capped CSV and retain only the rows needed for mapping."""

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

    if "\x00" in text:
        return _blocking("invalid_csv", "Invalid CSV: NUL bytes are not supported")

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
        if any(len(header) > MAX_CSV_HEADER_LENGTH for header in headers):
            return _blocking("invalid_csv", "CSV headers must be at most 512 characters")

        sample_rows: list[dict[str, str]] = []
        total_rows = 0
        reader = csv.DictReader(
            stream,
            fieldnames=headers,
            restkey=None,
            restval="",
            strict=True,
        )
        for raw_row in reader:
            if raw_row.get(None):
                return _blocking("invalid_csv", "CSV row has more values than the header")
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
