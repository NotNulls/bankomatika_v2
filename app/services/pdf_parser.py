"""Multi-bank PDF statement extraction.

The parser uses two complementary strategies:

1. ``pdfplumber`` table extraction with header heuristics for international and
   Balkan bank layouts.
2. A line-based regex fallback for statements whose columns are rendered as
   plain text rather than ruled table cells.

All monetary values are normalised to two-decimal floats and all dates to ISO
``YYYY-MM-DD`` strings, which makes exported spreadsheets predictable and
summable.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

import pdfplumber
from pypdf import PdfReader
from pypdf.errors import PdfReadError

CANONICAL_COLUMNS = (
    "date",
    "description",
    "reference",
    "debit",
    "credit",
    "balance",
)


class PdfParseError(ValueError):
    """A user-safe parsing failure."""

    code = "pdf_parse_error"


class EncryptedPdfError(PdfParseError):
    code = "pdf_encrypted"


class PageLimitExceeded(PdfParseError):
    code = "page_limit"


class NoTransactionsFound(PdfParseError):
    code = "no_transactions"


@dataclass
class ParsedStatement:
    page_count: int
    rows: list[dict[str, Any]]
    detected_bank: str = "Generic / International bank"
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bank identification
# ---------------------------------------------------------------------------

BANK_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("CKB", r"\bckb\b|crnogorska komercijalna banka"),
    ("NLB", r"\bnlb\b|nova ljubljanska banka|nlb banka"),
    ("Hipotekarna banka", r"hipotekarna banka"),
    ("Erste Bank", r"erste bank|erste\s*&\s*steierm"),
    ("Raiffeisen Bank", r"raiffeisen"),
    ("Banca Intesa", r"banca intesa|intesa sanpaolo"),
    ("UniCredit Bank", r"unicredit|unicredit bank"),
    ("Addiko Bank", r"addiko"),
    ("OTP Bank", r"\botp banka\b|\botp bank\b"),
    ("Poštanska štedionica", r"poštanska štedionica|postanska stedionica"),
    ("Halkbank", r"halkbank"),
    ("Sparkasse", r"sparkasse"),
    ("Zagrebačka banka", r"zagrebačka banka|zagrebacka banka"),
    ("Privredna banka Zagreb", r"privredna banka zagreb|\bpbz\b"),
    ("BKS Bank", r"\bbks bank\b"),
    ("Komercijalna banka", r"komercijalna banka"),
    ("NLB Komercijalna", r"nlb komercijalna"),
    ("Deutsche Bank", r"deutsche bank"),
    ("HSBC", r"\bhsbc\b"),
    ("JPMorgan Chase", r"jpmorgan|chase bank"),
    ("Bank of America", r"bank of america"),
    ("Wells Fargo", r"wells fargo"),
    ("Barclays", r"\bbarclays\b"),
    ("Santander", r"santander"),
    ("ING", r"\bing\b"),
    ("Revolut", r"\brevolut\b"),
    ("Monzo", r"\bmonzo\b"),
    ("N26", r"\bn26\b"),
    ("Wise", r"\bwise\b"),
)

_BANK_PATTERNS = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in BANK_SIGNATURES
)


def detect_bank(text: str) -> str:
    """Identify a bank from statement text using ordered header heuristics."""
    haystack = text or ""
    for name, pattern in _BANK_PATTERNS:
        if pattern.search(haystack):
            return name
    return "Generic / International bank"


# ---------------------------------------------------------------------------
# Field normalisation
# ---------------------------------------------------------------------------

HEADER_ALIASES: dict[str, set[str]] = {
    "date": {
        "date",
        "datum",
        "transaction date",
        "trans date",
        "transakcijski datum",
        "datum transakcije",
        "datum izvrsenja",
        "datum izvršenja",
        "datum knjizenja",
        "datum knjiženja",
        "booking date",
        "value date",
        "valuta",
        "processed date",
        "posted date",
    },
    "description": {
        "description",
        "opis",
        "opis transakcije",
        "details",
        "transaction details",
        "narrative",
        "transaction",
        "transaction description",
        "svrha",
        "svrha placanja",
        "svrha plaćanja",
        "purpose",
        "payment purpose",
        "memo",
        "particulars",
        "reference",
        "description/purpose",
    },
    "reference": {
        "reference",
        "referenca",
        "ref",
        "ref no",
        "reference number",
        "broj dokumenta",
        "document number",
        "transaction id",
        "transaction reference",
        "id",
        "broj transakcije",
    },
    "debit": {
        "debit",
        "debit amount",
        "debitamt",
        "zaduzenje",
        "zaduženje",
        "duguje",
        "isplata",
        "odliv",
        "money out",
        "withdrawal",
        "withdrawals",
        "paid out",
        "debit (eur)",
    },
    "credit": {
        "credit",
        "credit amount",
        "creditamt",
        "odobrenje",
        "potrazuje",
        "potražuje",
        "uplata",
        "priliv",
        "money in",
        "deposit",
        "deposits",
        "paid in",
        "credit (eur)",
    },
    "amount": {
        "amount",
        "iznos",
        "transaction amount",
        "value",
        "amount (eur)",
        "iznos transakcije",
        "amount in eur",
    },
    "balance": {
        "balance",
        "saldo",
        "stanje",
        "balance after",
        "running balance",
        "saldo nakon",
        "stanje nakon",
        "new balance",
        "closing balance",
    },
    "currency": {"currency", "valuta", "ccy", "currency code"},
}

_FIELD_ORDER = (
    "balance",
    "debit",
    "credit",
    "date",
    "reference",
    "description",
    "amount",
    "currency",
)

_NOISE_MARKERS = (
    "total",
    "subtotal",
    "ukupno",
    "ukupan",
    "ukupna",
    "zbir",
    "sum:",
    "opening balance",
    "closing balance",
    "pocetno stanje",
    "početno stanje",
    "saldo na dan",
)


def _strip_diacritics(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _normalise_header(value: Any) -> str:
    text = _cell_text(value).lower()
    text = _strip_diacritics(text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"[^a-z0-9/& ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _clean_description(value: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value or "")
    value = re.sub(r"\s+", " ", value).strip(" |;:-")
    return value[:2000]


def parse_amount(value: Any) -> float | None:
    """Parse ``1.250,50``, ``1,250.50``, ``(12.30)`` and similar formats."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None

    raw = (
        raw.replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("\u2212", "-")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
    )
    if raw.lower() in {"-", "—", "n/a", "na", "null", "none", ""}:
        return None

    is_negative = False
    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1]
    if re.search(r"(?:^|[\s(])-\s*\d", raw) or re.search(r"\d\s*-$", raw):
        is_negative = True
    if re.search(r"\b(?:dr|debit)\b", raw, re.IGNORECASE):
        is_negative = True
    if re.search(r"\b(?:cr|credit)\b", raw, re.IGNORECASE):
        is_negative = False

    raw = re.sub(r"[^\d.,]", "", raw)
    if not raw or not re.search(r"\d", raw):
        return None

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "." in raw:
        parts = raw.split(".")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            pass
        else:
            raw = raw.replace(".", "")

    try:
        amount = float(raw)
    except ValueError:
        return None
    if is_negative:
        amount = -abs(amount)
    return round(amount, 2)


_DATE_TOKEN_RE = re.compile(
    r"(?P<date>"
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2}"
    r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    r"|(?:\d{1,2}[\s-]+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*[\s,.-]+\d{2,4}"
    r")",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def normalise_date(value: Any) -> str | None:
    """Convert common European, Balkan and ISO date spellings to ISO format."""
    if value is None:
        return None
    text = _cell_text(value)
    if not text:
        return None
    text = text.replace("\u00a0", " ").strip().strip(".,;")
    text = re.sub(r"\s+\d{2}:\d{2}(?::\d{2})?\s*$", "", text)
    text = re.sub(r"\s+", " ", text)

    numeric = re.match(
        r"^(?P<a>\d{1,4})[./-](?P<b>\d{1,2})[./-](?P<c>\d{1,4})$", text
    )
    if numeric:
        a, b, c = (int(numeric.group(key)) for key in ("a", "b", "c"))
        try:
            if len(numeric.group("a")) == 4:
                year, month, day = a, b, c
            else:
                day, month, year = a, b, c
                if day > 12 and month > 12:
                    return None
                if month > 12 and day <= 12:
                    day, month = month, day
                if year < 100:
                    year += 2000 if year < 50 else 1900
            parsed = datetime(year, month, day)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            return None

    words = re.match(
        r"^(?P<day>\d{1,2})?[\s-]*(?P<month>[A-Za-z]+)[\s,.-]+(?P<year>\d{2,4})$",
        text,
    )
    if words:
        month_name = words.group("month").lower()
        month = _MONTH_NAMES.get(month_name)
        if month:
            try:
                day = int(words.group("day") or 1)
                year = int(words.group("year"))
                if year < 100:
                    year += 2000 if year < 50 else 1900
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                return None

    # Month-first spellings such as "Feb 1, 2024" and "February 1 2024".
    month_first = re.match(
        r"^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:,)?\s+(?P<year>\d{2,4})$",
        text,
    )
    if month_first:
        month = _MONTH_NAMES.get(month_first.group("month").lower())
        if month:
            try:
                day = int(month_first.group("day"))
                year = int(month_first.group("year"))
                if year < 100:
                    year += 2000 if year < 50 else 1900
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                return None
    return None


_REFERENCE_RE = re.compile(
    r"\b(?:REF|TX|TRX|TRN|ID|BROJ|DOKUMENT|DOC)[.:#/-]*\s*"
    r"([A-Z0-9][A-Z0-9/_-]{2,})\b",
    re.IGNORECASE,
)

_GENERIC_REFERENCE_RE = re.compile(
    r"\b("
    r"[A-Z]{2,}[A-Z0-9]*[-/][A-Z0-9]*\d[A-Z0-9-]*"
    r"|[A-Z]{2,}\d{4,}"
    r")\b",
    re.IGNORECASE,
)


def _extract_reference(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    match = _REFERENCE_RE.search(text)
    if not match:
        match = _GENERIC_REFERENCE_RE.search(text)
    if not match:
        return _clean_description(text), ""
    reference = match.group(0).strip(" .,;:-")
    description = (text[: match.start()] + " " + text[match.end() :]).strip()
    return _clean_description(description), reference


def _is_noise(text: str) -> bool:
    lowered = text.casefold().strip()
    if not lowered:
        return True
    return any(marker in lowered for marker in _NOISE_MARKERS)


def _row(
    date_value: str,
    description: str,
    reference: str,
    debit: float | None,
    credit: float | None,
    balance: float | None,
) -> dict[str, Any] | None:
    description = _clean_description(description)
    reference = _clean_description(reference)[:200]
    if not date_value:
        return None
    if not description:
        description = "Bank transaction"
    debit = round(abs(debit), 2) if debit not in (None, 0) else None
    credit = round(abs(credit), 2) if credit not in (None, 0) else None
    if balance is not None:
        balance = round(balance, 2)
    if debit is None and credit is None and balance is None:
        return None
    return {
        "date": date_value,
        "description": description,
        "reference": reference,
        "debit": debit,
        "credit": credit,
        "balance": balance,
    }


# ---------------------------------------------------------------------------
# Table extraction
# ---------------------------------------------------------------------------


def _detect_header(row: Iterable[Any]) -> dict[int, str] | None:
    cells = [_normalise_header(cell) for cell in row]
    if not cells or not any(cells):
        return None

    mapping: dict[int, str] = {}
    for index, header in enumerate(cells):
        if not header:
            continue
        exact = None
        partial = None
        for field in _FIELD_ORDER:
            aliases = HEADER_ALIASES[field]
            if header in aliases:
                exact = field
                break
            if partial is None and any(
                alias in header for alias in aliases if len(alias) >= 4
            ):
                partial = field
        if exact:
            mapping[index] = exact
        elif partial:
            mapping[index] = partial

    fields = set(mapping.values())
    has_date = "date" in fields
    has_value = bool(fields & {"amount", "debit", "credit", "balance"})
    has_description = "description" in fields
    if not (has_date and has_value and (has_description or "reference" in fields)):
        return None
    # Debit and credit columns take precedence over a generic "amount" column.
    if "debit" in fields or "credit" in fields:
        mapping = {index: field for index, field in mapping.items() if field != "amount"}
    return mapping


def _mapped_values(row: list[Any], mapping: dict[int, str]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for index, field_name in mapping.items():
        if index >= len(row):
            continue
        raw = _cell_text(row[index])
        if not raw:
            continue
        values.setdefault(field_name, []).append(raw)
    return values


def _rows_from_table(
    table: list[list[Any]], page_number: int, warnings: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    header_index: int | None = None
    mapping: dict[int, str] = {}
    last_date: str | None = None
    previous_balance: float | None = None

    for index, raw_row in enumerate(table):
        header = _detect_header(raw_row)
        if header:
            header_index = index
            mapping = header
            break
    if header_index is None or not mapping:
        return rows

    for raw_row in table[header_index + 1 :]:
        values = _mapped_values(raw_row, mapping)
        joined = " ".join(_cell_text(cell) for cell in raw_row)
        if _is_noise(joined) and not values.get("date"):
            continue

        date_cell = " ".join(values.get("date", []))
        date_value = normalise_date(date_cell)
        if date_value:
            last_date = date_value
        elif date_cell:
            # It might be a transaction type in the date column, not a date.
            date_value = None

        description_parts = values.get("description", [])
        description = " ".join(description_parts)
        reference = " ".join(values.get("reference", []))

        debit = parse_amount(values.get("debit", [None])[0])
        credit = parse_amount(values.get("credit", [None])[0])
        amount = parse_amount(values.get("amount", [None])[0])
        balance = parse_amount(values.get("balance", [None])[0])

        if not date_value:
            if date_cell and not normalise_date(date_cell):
                continue
            if not date_value and description and not any(
                value is not None for value in (debit, credit, amount, balance)
            ):
                # A wrapped description row - append it to the previous row.
                if rows and len(rows[-1]["description"]) < 1800:
                    rows[-1]["description"] = _clean_description(
                        f'{rows[-1]["description"]} {description}'
                    )
                continue
            if last_date and any(
                value is not None for value in (debit, credit, amount, balance)
            ):
                date_value = last_date
            else:
                continue

        if not description:
            description = " ".join(
                _cell_text(raw_row[i])
                for i in range(len(raw_row))
                if i not in mapping
            )
        description, extracted_reference = _extract_reference(description)
        if not reference:
            reference = extracted_reference

        if amount is not None and debit is None and credit is None:
            if amount < 0:
                debit = abs(amount)
            elif previous_balance is not None and balance is not None:
                delta = round(balance - previous_balance, 2)
                if delta < 0:
                    debit = amount
                elif delta > 0:
                    credit = amount
                else:
                    credit = amount
            else:
                credit = amount
        elif amount is not None and (debit is not None or credit is not None):
            if debit is None and credit is None:
                debit = amount if amount < 0 else None
                credit = abs(amount) if amount > 0 else None

        normalised = _row(
            date_value, description, reference, debit, credit, balance
        )
        if normalised:
            rows.append(normalised)
            if balance is not None:
                previous_balance = balance
    if rows:
        warnings.append(f"Extracted {len(rows)} rows from a table on page {page_number}.")
    return rows


_TABLE_SETTINGS = (
    {},
    {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "intersection_tolerance": 8,
    },
)


def _extract_table_rows(page: Any, page_number: int, warnings: list[str]) -> list[dict[str, Any]]:
    for settings in _TABLE_SETTINGS:
        try:
            tables = page.extract_tables(settings)
        except Exception:  # pdfplumber can be brittle on malformed grids
            continue
        rows: list[dict[str, Any]] = []
        for table in tables or []:
            if not table:
                continue
            rows.extend(_rows_from_table(table, page_number, warnings))
        if rows:
            return rows
    return []


# ---------------------------------------------------------------------------
# Line-based fallback
# ---------------------------------------------------------------------------

_LINE_DATE_RE = re.compile(
    r"^\s*[|]?\s*(?P<date>"
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2}"
    r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    r"|(?:\d{1,2}[\s-]+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*[\s,.-]+\d{2,4}"
    r")\b",
    re.IGNORECASE,
)

_LINE_AMOUNT_RE = re.compile(
    r"(?<![\w/.,-])"
    r"(?P<amount>"
    r"\(?[-+]?"
    r"(?:\d{1,3}(?:[ .\u00a0]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?)"
    r"\)?-?"
    r")"
    r"(?![\w.,])"
)


def _line_amounts(rest: str) -> list[tuple[float, int, int, str]]:
    found: list[tuple[float, int, int, str]] = []
    for match in _LINE_AMOUNT_RE.finditer(rest):
        raw = match.group("amount")
        value = parse_amount(raw)
        if value is None:
            continue
        before = rest[max(0, match.start() - 5) : match.start()]
        if re.search(r"(?:ref|tx|trx|id)[-: ]*$", before, re.IGNORECASE):
            continue
        found.append((value, match.start(), match.end(), raw))
    return found


def _parse_text_line(line: str) -> dict[str, Any] | None:
    cleaned_line = line.replace("\u00a0", " ").strip()
    if not cleaned_line:
        return None
    date_match = _LINE_DATE_RE.match(cleaned_line)
    if not date_match:
        return None
    date_value = normalise_date(date_match.group("date"))
    if not date_value:
        return None

    rest = cleaned_line[date_match.end() :].strip()
    if not rest:
        return None
    tokens = _line_amounts(rest)
    if not tokens:
        return None

    # Use the trailing amounts; descriptions often contain incidental numbers.
    if len(tokens) >= 3:
        selected = tokens[-3:]
        raw_debit, raw_credit, raw_balance = (token[0] for token in selected)
        debit = abs(raw_debit) if raw_debit else None
        credit = abs(raw_credit) if raw_credit else None
        balance = raw_balance
        if debit and credit:
            # Both populated is unusual; keep them both rather than guessing.
            pass
        elif debit is None and credit is None:
            return None
        description_end = selected[0][1]
    elif len(tokens) == 2:
        amount, balance = tokens[-2][0], tokens[-1][0]
        description_end = tokens[-2][1]
        debit = abs(amount) if amount < 0 else None
        credit = amount if amount > 0 else None
        if debit is None and credit is None:
            return None
    else:
        amount = tokens[-1][0]
        balance = None
        description_end = tokens[-1][1]
        debit = abs(amount) if amount < 0 else None
        credit = amount if amount > 0 else None
        if debit is None and credit is None:
            return None

    description = rest[:description_end].strip(" |;:-")
    if _is_noise(description):
        return None
    description, reference = _extract_reference(description)
    return _row(date_value, description, reference, debit, credit, balance)


def _extract_line_rows(
    text: str, page_number: int, warnings: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending_date: str | None = None
    pending_description = ""

    for raw_line in text.splitlines():
        line = raw_line.replace("\u00a0", " ").strip()
        if not line:
            continue
        parsed = _parse_text_line(line)
        if parsed:
            rows.append(parsed)
            pending_date = None
            pending_description = ""
            continue

        date_match = _LINE_DATE_RE.match(line)
        if date_match:
            date_value = normalise_date(date_match.group("date"))
            if date_value:
                pending_date = date_value
                pending_description = line[date_match.end() :].strip(" |;:-")
                continue
        if pending_date and pending_description and not _is_noise(line):
            pending_description = f"{pending_description} {line}".strip()
        elif pending_date and pending_description and len(pending_description) > 2000:
            pending_date = None
            pending_description = ""

    if not rows and "Rbr" in text and "Odliv" in text and "Priliv" in text:
        rows = _extract_leading_id_rows(text, page_number, warnings)

    if rows:
        warnings.append(f"Used line-based extraction on page {page_number}.")
    return rows


# OTP Banka (and similar) statements place the transaction number, ID and
# account before two dates, debit/credit columns and the reference.  The dates
# therefore do not start the line, which the generic parser above cannot see.
_LEADING_ID_AMOUNT = (
    r"\(?[-+]?\d{1,3}(?:[ .\u00a0]\d{3})*(?:[.,]\d{2})\)?-?"
)
_LEADING_ID_TRANSACTION_RE = re.compile(
    r"^\s*(?P<rbr>\d{1,4})\s+"
    r"(?P<id>[A-Z0-9]{5,})\s+"
    r"(?P<account>\d{8,})\s+"
    r"(?P<date1>\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+"
    r"(?P<date2>\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+"
    r"(?P<debit>" + _LEADING_ID_AMOUNT + r")\s+"
    r"(?P<credit>" + _LEADING_ID_AMOUNT + r")"
    r"(?:\s+(?P<rest>.+))?$",
    re.IGNORECASE,
)


def _extract_leading_id_rows(
    text: str, page_number: int, warnings: list[str]
) -> list[dict[str, Any]]:
    lines = text.splitlines()
    rows: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.replace("\u00a0", " ").strip()
        match = _LEADING_ID_TRANSACTION_RE.match(line)
        if not match:
            continue
        date_value = normalise_date(match.group("date1"))
        if not date_value:
            continue
        debit = parse_amount(match.group("debit"))
        credit = parse_amount(match.group("credit"))
        reference = (match.group("rest") or "").strip()
        description_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            next_line = lines[cursor].replace("\u00a0", " ").strip()
            if not next_line:
                cursor += 1
                continue
            if _LEADING_ID_TRANSACTION_RE.match(next_line):
                break
            if re.match(
                r"^(?:UKUPNO|Ukupno|Strana|Rbr|Prethodno stanje|Novo stanje)\b",
                next_line,
                re.IGNORECASE,
            ):
                break
            description_lines.append(next_line)
            if len(" ".join(description_lines)) > 1800:
                break
            cursor += 1
        description = _clean_description(" ".join(description_lines))
        if not reference:
            description, reference = _extract_reference(description)
        normalised = _row(
            date_value,
            description,
            reference,
            debit,
            credit,
            None,
        )
        if normalised:
            rows.append(normalised)
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def count_pdf_pages(pdf_bytes: bytes) -> int:
    """Return the exact page count, rejecting encrypted or invalid PDFs."""
    if not pdf_bytes:
        raise PdfParseError("The uploaded PDF is empty.")
    if not pdf_bytes.lstrip().startswith(b"%PDF-"):
        raise PdfParseError("The uploaded file is not a valid PDF document.")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    except (PdfReadError, ValueError, OSError) as exc:
        raise PdfParseError("The PDF could not be opened. It may be corrupted.") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise EncryptedPdfError(
                "This PDF is password protected. Please upload an unlocked statement."
            ) from exc
        if reader.is_encrypted:
            raise EncryptedPdfError(
                "This PDF is password protected. Please upload an unlocked statement."
            )
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        raise PdfParseError("The PDF page count could not be read.") from exc
    if page_count < 1:
        raise PdfParseError("The PDF has no pages.")
    return page_count


def _extract_page_text(page: Any) -> str:
    try:
        return page.extract_text(x_tolerance=2, y_tolerance=3) or ""
    except Exception:
        try:
            return page.extract_text() or ""
        except Exception:
            return ""


def parse_statement(
    pdf_bytes: bytes,
    filename: str = "",
    max_pages: int = 500,
    max_rows: int = 50000,
) -> ParsedStatement:
    """Parse all pages of a bank statement.

    Raises :class:`PdfParseError` subclasses for user-facing failures.
    """
    page_count = count_pdf_pages(pdf_bytes)
    if page_count > max_pages:
        raise PageLimitExceeded(
            f"This statement has {page_count} pages; the maximum is {max_pages} pages."
        )

    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    page_texts: list[str] = []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                text = _extract_page_text(page)
                page_texts.append(text)
                table_rows = _extract_table_rows(page, page_number, warnings)
                if table_rows:
                    # Some bank exports visually contain a balance column that
                    # the table detector misses.  If every table row is missing
                    # its balance, try the text layout and use it when it
                    # recovers more complete transactions.
                    if all(row.get("balance") is None for row in table_rows):
                        line_rows = _extract_line_rows(text, page_number, [])
                        if line_rows and any(
                            row.get("balance") is not None for row in line_rows
                        ):
                            table_rows = line_rows
                    rows.extend(table_rows)
                else:
                    rows.extend(_extract_line_rows(text, page_number, warnings))
                if len(rows) > max_rows:
                    raise PdfParseError(
                        "This statement contains too many transactions to convert "
                        f"(limit: {max_rows:,})."
                    )
    except PdfParseError:
        raise
    except Exception as exc:
        raise PdfParseError(
            "The PDF could not be parsed. It may be corrupted or contain only scans."
        ) from exc

    if not rows:
        if not any(text.strip() for text in page_texts):
            raise NoTransactionsFound(
                "No extractable text was found. Scanned image PDFs are not supported; "
                "please upload a text-based bank statement."
            )
        raise NoTransactionsFound(
            "No transaction rows were detected. The bank layout may not be supported yet."
        )

    detected_bank = detect_bank("\n".join(page_texts))
    if detected_bank == "Generic / International bank":
        warnings.append("Bank not recognised; generic table heuristics were used.")
    return ParsedStatement(
        page_count=page_count,
        rows=rows,
        detected_bank=detected_bank,
        warnings=warnings,
    )