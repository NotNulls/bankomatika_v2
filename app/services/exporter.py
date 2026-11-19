"""Formatted Excel and CSV export generation.

Exports intentionally avoid locale-dependent number strings so spreadsheet
formulas and accounting imports can consume the values directly.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from werkzeug.utils import secure_filename

from app.services.pdf_parser import normalise_date, parse_amount

EXPORT_COLUMNS = (
    ("date", "Date"),
    ("description", "Description"),
    ("reference", "Reference"),
    ("debit", "Debit"),
    ("credit", "Credit"),
    ("balance", "Balance"),
)

XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIMETYPE = "text/csv; charset=utf-8"
MAX_EXPORT_ROWS = 50000

_HEADER_FILL = PatternFill("solid", fgColor="0B1F3A")
_ACCENT_FILL = PatternFill("solid", fgColor="E8F7F1")
_HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
_TOTAL_FONT = Font(name="Calibri", size=11, bold=True, color="0B1F3A")
_BODY_FONT = Font(name="Calibri", size=11, color="1E293B")
_THIN_BORDER = Border(
    bottom=Side(style="thin", color="E2E8F0"),
    top=Side(style="thin", color="E2E8F0"),
)


class ExportError(ValueError):
    pass


def _coerce_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date,)):
        return value.strftime("%Y-%m-%d")
    normalised = normalise_date(value)
    if normalised:
        return normalised
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return value.strip()
    return ""


def _coerce_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    parsed = parse_amount(value)
    if parsed is None:
        return None
    return round(abs(parsed), 2)


def normalise_export_rows(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Validate and normalise rows coming from the browser or API."""
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ExportError("Rows must be provided as a list.")
    if len(rows) > MAX_EXPORT_ROWS:
        raise ExportError(f"A maximum of {MAX_EXPORT_ROWS:,} rows can be exported.")
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        description = str(row.get("description") or "").strip()[:2000]
        reference = str(row.get("reference") or "").strip()[:200]
        clean.append(
            {
                "date": _coerce_date(row.get("date")),
                "description": description or "Bank transaction",
                "reference": reference,
                "debit": _coerce_amount(row.get("debit")),
                "credit": _coerce_amount(row.get("credit")),
                "balance": _coerce_amount(row.get("balance")),
            }
        )
    return clean


def download_filename(original_filename: str | None, extension: str) -> str:
    stem = secure_filename((original_filename or "statement").rsplit("/", 1)[-1])
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)
    stem = (stem or "statement")[:90].strip("._-")
    return f"{stem}-transactions.{extension}"


def build_xlsx(
    rows: Iterable[dict[str, Any]], original_filename: str | None = None
) -> tuple[io.BytesIO, str]:
    """Build a formatted workbook and return ``(buffer, filename)``."""
    clean_rows = normalise_export_rows(rows)

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Transactions"
    worksheet.sheet_view.showGridLines = False
    worksheet.freeze_panes = "A2"

    for column_index, (_key, label) in enumerate(EXPORT_COLUMNS, start=1):
        cell = worksheet.cell(row=1, column=column_index, value=label)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="left", vertical="center")
        cell.border = _THIN_BORDER
    worksheet.row_dimensions[1].height = 24

    for row_index, row in enumerate(clean_rows, start=2):
        values = {
            "date": row["date"],
            "description": row["description"],
            "reference": row["reference"],
            "debit": row["debit"],
            "credit": row["credit"],
            "balance": row["balance"],
        }
        for column_index, (key, _label) in enumerate(EXPORT_COLUMNS, start=1):
            cell = worksheet.cell(row=row_index, column=column_index, value=values[key])
            cell.font = _BODY_FONT
            cell.border = _THIN_BORDER
            if key in {"debit", "credit", "balance"}:
                cell.number_format = '#,##0.00;[Red]-#,##0.00'
                cell.alignment = Alignment(horizontal="right")
            elif key == "date":
                cell.alignment = Alignment(horizontal="center")
            else:
                cell.alignment = Alignment(vertical="top", wrap_text=False)
        if row_index % 2 == 0:
            for column_index in range(1, len(EXPORT_COLUMNS) + 1):
                worksheet.cell(row=row_index, column=column_index).fill = _ACCENT_FILL

    last_data_row = len(clean_rows) + 1
    if clean_rows:
        worksheet.auto_filter.ref = f"A1:F{last_data_row}"

        # Keep the transaction sheet clean for ledger imports; totals live on a
        # separate summary tab so they never pollute an import range.
        summary = workbook.create_sheet("Summary")
        summary.sheet_view.showGridLines = False
        summary["A1"] = "Statement summary"
        summary["A1"].font = Font(name="Calibri", size=14, bold=True, color="0B1F3A")
        summary["A3"] = "Converted transactions"
        summary["B3"] = len(clean_rows)
        summary["A4"] = "Total debit"
        summary["B4"] = f"=SUM(Transactions!D2:D{last_data_row})"
        summary["A5"] = "Total credit"
        summary["B5"] = f"=SUM(Transactions!E2:E{last_data_row})"
        summary["A6"] = "Net movement"
        summary["B6"] = "=B5-B4"
        summary["A7"] = "Closing balance"
        summary["B7"] = clean_rows[-1]["balance"]
        for row_index in range(3, 8):
            summary.cell(row=row_index, column=1).font = _BODY_FONT
            summary.cell(row=row_index, column=2).font = _TOTAL_FONT
            summary.cell(row=row_index, column=1).border = _THIN_BORDER
            summary.cell(row=row_index, column=2).border = _THIN_BORDER
        for row_index in (4, 5, 6, 7):
            summary.cell(row=row_index, column=2).number_format = '#,##0.00;[Red]-#,##0.00'
        summary.column_dimensions["A"].width = 28
        summary.column_dimensions["B"].width = 20

    widths = {"A": 14, "B": 52, "C": 22, "D": 16, "E": 16, "F": 18}
    for column, width in widths.items():
        worksheet.column_dimensions[column].width = width

    worksheet.print_title_rows = "1:1"
    worksheet.page_setup.orientation = "landscape"
    worksheet.page_setup.fitToWidth = 1
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.oddFooter.center.text = "Generated by Bank Statement Converter"
    worksheet.oddFooter.center.size = 8

    workbook.properties.title = "Converted bank statement"
    workbook.properties.creator = "Bank Statement Converter"
    workbook.properties.subject = original_filename or "Bank statement"
    workbook.properties.keywords = "bank statement, transactions, accounting"

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer, download_filename(original_filename, "xlsx")


def build_csv(
    rows: Iterable[dict[str, Any]], original_filename: str | None = None
) -> tuple[io.BytesIO, str]:
    """Build a UTF-8 BOM CSV that opens cleanly in Excel."""
    clean_rows = normalise_export_rows(rows)
    buffer = io.BytesIO()
    text_buffer = io.TextIOWrapper(buffer, encoding="utf-8-sig", newline="")
    writer = csv.writer(text_buffer, lineterminator="\r\n")
    writer.writerow([label for _key, label in EXPORT_COLUMNS])
    for row in clean_rows:
        writer.writerow(
            [
                row["date"],
                row["description"],
                row["reference"],
                f"{row['debit']:.2f}" if row["debit"] is not None else "",
                f"{row['credit']:.2f}" if row["credit"] is not None else "",
                f"{row['balance']:.2f}" if row["balance"] is not None else "",
            ]
        )
    text_buffer.flush()
    text_buffer.detach()
    buffer.seek(0)
    return buffer, download_filename(original_filename, "csv")


def build_export(
    export_format: str,
    rows: Iterable[dict[str, Any]],
    original_filename: str | None = None,
) -> tuple[io.BytesIO, str, str]:
    export_format = (export_format or "").lower()
    if export_format in {"xlsx", "xls", "excel"}:
        buffer, filename = build_xlsx(rows, original_filename)
        return buffer, filename, XLSX_MIMETYPE
    if export_format == "csv":
        buffer, filename = build_csv(rows, original_filename)
        return buffer, filename, CSV_MIMETYPE
    raise ExportError("Unsupported export format. Choose xlsx or csv.")
