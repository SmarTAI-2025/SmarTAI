"""Read student rosters only; submission and teaching-material readers are separate."""
from __future__ import annotations

import csv
import io
from pathlib import PurePath

from backend.domain.errors import ValidationError


_ID_HEADERS = {"student_id", "stu_id", "学号"}
_NAME_HEADERS = {"student_name", "stu_name", "name", "姓名"}


def _columns(header: list[str]) -> tuple[int, int | None] | None:
    normalized = [cell.strip().casefold() for cell in header]
    ids = [index for index, value in enumerate(normalized) if value in _ID_HEADERS]
    names = [index for index, value in enumerate(normalized) if value in _NAME_HEADERS]
    # Ambiguous identity columns must not silently overwrite one another.
    return (ids[0], names[0] if names else None) if len(ids) == 1 and len(names) <= 1 else None


def _reader(text: str, delimiter: str):
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    header = next((row for row in reader if any(cell.strip() for cell in row)), [])
    return reader, header


def parse_roster(body: bytes, *, filename: str = "roster.csv") -> list[dict[str, str]]:
    """CSV uses commas, TSV uses tabs, and TXT accepts either headed table.

    Delimiter detection is restricted to the header, so commas/tabs inside
    quoted names or notes cannot change the interpretation of the whole file.
    """
    suffix = PurePath(filename).suffix.casefold()
    if suffix not in {".csv", ".tsv", ".txt"}:
        raise ValidationError(code="submission_roster_unsupported")
    try:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = body.decode("gbk")
    except UnicodeDecodeError as exc:
        raise ValidationError(code="submission_roster_decode_failed") from exc
    if not text.strip():
        raise ValidationError(code="submission_roster_empty")
    if "\x00" in text:
        raise ValidationError(code="submission_roster_invalid")

    delimiter = "\t" if suffix == ".tsv" else ","
    try:
        # A quoted TSV header can be invalid CSV syntax (and vice versa),
        # so a failed header parse must still allow the alternate TXT format.
        try:
            reader, header = _reader(text, delimiter)
        except csv.Error:
            reader, header = None, []
        columns = _columns(header)
        if columns is None:
            try:
                alternate_reader, alternate_header = _reader(text, "\t" if delimiter == "," else ",")
            except csv.Error:
                alternate_reader, alternate_header = None, []
            alternate_columns = _columns(alternate_header)
            if alternate_columns is not None:
                if suffix != ".txt":
                    raise ValidationError(code="submission_roster_delimiter_mismatch")
                reader, header, columns = alternate_reader, alternate_header, alternate_columns
            else:
                raise ValidationError(code="submission_roster_invalid" if reader is None else "submission_roster_headers_invalid")
        student_id_column, student_name_column = columns
        items = []
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(header) or not row[student_id_column].strip():
                raise ValidationError(code="submission_roster_rows_invalid", details={"line": reader.line_num})
            items.append({
                "stu_id": row[student_id_column].strip(),
                "stu_name": row[student_name_column].strip() if student_name_column is not None else "",
            })
    except csv.Error as exc:
        raise ValidationError(code="submission_roster_invalid") from exc
    if not items:
        raise ValidationError(code="submission_roster_empty")
    return items
