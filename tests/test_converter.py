"""End-to-end conversion tests.

The suite builds a one-page CKB-style PDF in memory, posts it through the real
upload route, checks the parsed JSON, downloads both formats, and confirms the
second anonymous request in 24 hours is rejected by the page quota.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from openpyxl import load_workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app import create_app
from app.extensions import db
from app.models import (
    PLAN_BUSINESS,
    ApiKey,
    UsageLog,
    User,
    add_one_month,
    utcnow,
)
from app.services.pdf_parser import parse_statement


def _draw_cell_text(pdf, text: str, x: float, y: float, right: float | None = None) -> None:
    pdf.setFont("Helvetica", 8)
    if right is None:
        pdf.drawString(x + 4, y + 8, text)
    else:
        pdf.drawRightString(right - 4, y + 8, text)


def build_sample_statement() -> bytes:
    """Create a ruled one-page bank statement with CKB-style columns."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(40, height - 52, "CKB Banka AD Podgorica")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(40, height - 70, "Account: ME25501000001234567890")
    pdf.drawString(40, height - 84, "Statement period: 01.02.2024 - 29.02.2024")
    pdf.drawString(40, height - 98, "Currency: EUR")

    columns = [35, 110, 300, 395, 465, 520, 575]
    top = height - 145
    row_height = 24
    headers = ["Datum", "Opis", "Referenca", "Duguje", "Potrazuje", "Saldo"]
    rows = [
        ["01.02.2024", "Grocery Market", "REF-001", "1.250,50", "0,00", "15.000,00"],
        ["02.02.2024", "Salary February", "SAL-002", "0,00", "2.500,00", "17.500,00"],
        ["03.02.2024", "Rent February", "RENT-03", "450,00", "0,00", "17.050,00"],
    ]

    # Header background and text.
    pdf.setFillColorRGB(0.92, 0.95, 0.98)
    pdf.rect(columns[0], top - row_height, columns[-1] - columns[0], row_height, fill=1, stroke=0)
    pdf.setFillColorRGB(0, 0, 0)
    for index, header in enumerate(headers):
        _draw_cell_text(
            pdf,
            header,
            columns[index],
            top - row_height,
            columns[index + 1] if index + 1 < len(columns) else None,
        )

    # Grid + data rows.
    for row_index, row in enumerate(rows, start=1):
        baseline = top - (row_index + 1) * row_height
        for col_index, value in enumerate(row):
            right = columns[col_index + 1] if col_index + 1 < len(columns) else None
            _draw_cell_text(pdf, value, columns[col_index], baseline, right)

    pdf.setStrokeColorRGB(0.72, 0.78, 0.85)
    for row_index in range(len(rows) + 2):
        y = top - row_index * row_height
        pdf.line(columns[0], y, columns[-1], y)
    for x in columns:
        pdf.line(x, top, x, top - (len(rows) + 1) * row_height)

    pdf.setFillColorRGB(0.35, 0.4, 0.47)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(40, 50, "Generated sample statement for automated tests. Page 1 of 1.")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def build_plain_statement() -> bytes:
    """Create a statement with no ruled table, exercising the regex fallback."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    lines = [
        "CKB Banka AD Podgorica",
        "Datum Opis Referenca Duguje Potrazuje Saldo",
        "01.02.2024 Grocery Market REF-001 1.250,50 0,00 15.000,00",
        "02.02.2024 Salary February SAL-002 0,00 2.500,00 17.500,00",
        "03.02.2024 Rent February RENT-03 450,00 0,00 17.050,00",
    ]
    for line in lines:
        pdf.drawString(40, y, line)
        y -= 22
    pdf.save()
    return buffer.getvalue()


@pytest.fixture()
def app():
    application = create_app("testing")
    upload_dir = Path(application.root_path).parent / "instance" / "test-uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    for existing in upload_dir.glob("*.pdf"):
        existing.unlink()
    application.config.update(
        UPLOAD_FOLDER=str(upload_dir),
        CLEANUP_INTERVAL_SECONDS=999999,
        SERVER_NAME=None,
    )
    with application.app_context():
        db.create_all()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()
    for existing in upload_dir.glob("*.pdf"):
        existing.unlink()


@pytest.fixture()
def client(app):
    return app.test_client()


def test_full_conversion_flow_and_anonymous_quota(client, app):
    home = client.get("/")
    assert home.status_code == 200
    assert b"The world" in home.data
    assert b"Download Excel" in home.data

    sample_pdf = build_sample_statement()
    assert sample_pdf.startswith(b"%PDF-")

    response = client.post(
        "/convert",
        data={"file": (io.BytesIO(sample_pdf), "ckb-sample.pdf")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["page_count"] == 1
    assert payload["detected_bank"] == "CKB"
    assert payload["row_count"] == 3
    assert payload["quota"]["scope"] == "anonymous"
    assert payload["quota"]["used"] == 1
    assert payload["quota"]["remaining"] == 0

    rows = payload["rows"]
    assert len(rows) == 3
    assert rows[0]["date"] == "2024-02-01"
    assert rows[0]["description"] == "Grocery Market"
    assert rows[0]["reference"] == "REF-001"
    assert rows[0]["debit"] == pytest.approx(1250.50)
    assert rows[0]["credit"] is None
    assert rows[0]["balance"] == pytest.approx(15000.00)
    assert rows[1]["credit"] == pytest.approx(2500.00)
    assert rows[2]["debit"] == pytest.approx(450.00)

    # Download the generated XLSX and verify numeric cells, not strings.
    xlsx_url = payload["downloads"]["xlsx"]
    xlsx_response = client.get(xlsx_url)
    assert xlsx_response.status_code == 200
    assert "spreadsheetml" in xlsx_response.content_type
    workbook = load_workbook(io.BytesIO(xlsx_response.data))
    sheet = workbook["Transactions"]
    assert sheet["A1"].value == "Date"
    assert sheet["D1"].value == "Debit"
    assert sheet["A2"].value == "2024-02-01"
    assert sheet["B2"].value == "Grocery Market"
    assert sheet["D2"].value == pytest.approx(1250.50)
    assert sheet["E3"].value == pytest.approx(2500.00)
    assert sheet.max_row == 4  # header + three clean transaction rows
    assert workbook["Summary"]["B4"].value.startswith("=SUM(Transactions!")

    # CSV uses a UTF-8 BOM and contains the same reviewed rows.
    csv_response = client.get(payload["downloads"]["csv"])
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.content_type
    csv_text = csv_response.data.decode("utf-8-sig")
    assert "Grocery Market" in csv_text
    assert "1250.50" in csv_text

    # Edits made in the browser are sent with the download and reflected in Excel.
    edited_rows = [dict(rows[0])]
    edited_rows[0]["description"] = "Grocery Market (edited)"
    edited_response = client.post(
        payload["downloads"]["xlsx"],
        json={"rows": edited_rows},
        headers={"Accept": "application/json"},
    )
    assert edited_response.status_code == 200
    edited_book = load_workbook(io.BytesIO(edited_response.data))
    assert edited_book["Transactions"]["B2"].value == "Grocery Market (edited)"

    # The raw upload must be deleted after every request.
    upload_dir = Path(app.config["UPLOAD_FOLDER"])
    assert list(upload_dir.glob("*.pdf")) == []

    # A second anonymous conversion inside the same 24-hour window is blocked.
    second = client.post(
        "/convert",
        data={"file": (io.BytesIO(sample_pdf), "ckb-sample-2.pdf")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert second.status_code == 429
    second_payload = second.get_json()
    assert second_payload["error"] == "quota_exceeded"
    assert second_payload["quota"]["used"] == 1
    assert list(upload_dir.glob("*.pdf")) == []

    with app.app_context():
        assert UsageLog.query.count() == 1


def test_balkan_locale_detection_and_switching(client):
    montenegro = client.get(
        "/",
        headers={"CF-IPCountry": "ME", "Accept-Language": "en-US,en;q=0.9"},
    )
    assert montenegro.status_code == 200
    assert "Najpouzdaniji svjetski" in montenegro.get_data(as_text=True)

    croatian = client.get("/?lang=hr")
    assert croatian.status_code == 200
    assert "pretvarač bankovnih izvoda" in croatian.get_data(as_text=True)

    bosnian = client.get("/?lang=bs")
    assert bosnian.status_code == 200
    assert "konverter bankovnih izvoda" in bosnian.get_data(as_text=True)

    english = client.get("/?lang=en")
    assert english.status_code == 200
    assert "The world's most trusted" in english.get_data(as_text=True)


def test_api_requires_bearer_key_and_converts(client, app):
    with app.app_context():
        user = User(
            email="api-test@example.com",
            full_name="API Tester",
            plan_tier=PLAN_BUSINESS,
            monthly_page_quota=2500,
            pages_used_this_cycle=0,
            cycle_reset_at=add_one_month(utcnow()),
            preferred_lang="en",
        )
        user.set_password("demo1234")
        db.session.add(user)
        db.session.flush()
        api_key, raw_key = ApiKey.issue(user, name="Test key")
        db.session.add(api_key)
        db.session.commit()
        user_id = user.id

    sample_pdf = build_sample_statement()
    missing_key = client.post(
        "/api/v1/convert",
        data={"file": (io.BytesIO(sample_pdf), "api.pdf")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert missing_key.status_code == 401
    assert missing_key.get_json()["error"] == "missing_api_key"

    response = client.post(
        "/api/v1/convert",
        data={"file": (io.BytesIO(sample_pdf), "api.pdf")},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {raw_key}", "Accept": "application/json"},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["api_version"] == "v1"
    assert payload["row_count"] == 3
    assert payload["rows"][0]["description"] == "Grocery Market"

    with app.app_context():
        refreshed = db.session.get(User, user_id)
        assert refreshed.pages_used_this_cycle == 1
        stored_key = ApiKey.query.filter_by(user_id=user_id).first()
        assert stored_key.last_used_at is not None


def test_registered_free_daily_quota(client, app):
    registration = client.post(
        "/auth/register",
        data={
            "full_name": "Free User",
            "email": "free-test@example.com",
            "password": "demo1234",
            "confirm_password": "demo1234",
            "lang": "en",
        },
        follow_redirects=True,
    )
    assert registration.status_code == 200
    assert b"5 pages every 24 hours" in registration.data

    sample_pdf = build_sample_statement()
    for index in range(5):
        response = client.post(
            "/convert",
            data={"file": (io.BytesIO(sample_pdf), f"free-{index}.pdf")},
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200, response.get_data(as_text=True)

    blocked = client.post(
        "/convert",
        data={"file": (io.BytesIO(sample_pdf), "free-blocked.pdf")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert blocked.status_code == 429
    assert blocked.get_json()["quota"]["scope"] == "free"
    assert blocked.get_json()["quota"]["used"] == 5


def test_line_based_fallback_without_ruled_table():
    parsed = parse_statement(build_plain_statement())
    assert parsed.page_count == 1
    assert parsed.detected_bank == "CKB"
    assert len(parsed.rows) == 3
    assert parsed.rows[0]["date"] == "2024-02-01"
    assert parsed.rows[0]["description"] == "Grocery Market"
    assert parsed.rows[0]["reference"] == "REF-001"
    assert parsed.rows[0]["debit"] == pytest.approx(1250.50)
    assert parsed.rows[0]["credit"] is None
    assert parsed.rows[0]["balance"] == pytest.approx(15000.00)
    assert parsed.rows[1]["reference"] == "SAL-002"
    assert parsed.rows[1]["credit"] == pytest.approx(2500.00)


