# Bank Statement Converter

A production-oriented Flask + PostgreSQL SaaS for converting PDF bank statements into clean, editable Excel (`.xlsx`) and CSV files. It includes a drag-and-drop uploader with an inline-editable preview, anonymous/free/paid quotas, a user dashboard, API key management, and a Bearer-authenticated REST API.

## Highlights

- **PDF to clean Excel/CSV** — `pdfplumber` table extraction plus a line-based regex fallback, `pypdf` page counting and `openpyxl` export.
- **Balkan + international bank layouts** — header heuristics for CKB, NLB, Hipotekarna, Erste, Raiffeisen, Intesa, UniCredit, Addiko, OTP, Sparkasse and generic international formats.
- **European number/date normalisation** — `1.250,50` → `1250.50`; `01.02.2024` or `2024-02-01` → `2024-02-01`; numeric cells stay numeric in Excel.
- **Zero retention** — every uploaded PDF is deleted in a `finally` block immediately after parsing; parsed preview payloads are purged after one hour.
- **Quotas** — 1 page/24h anonymous, 5 pages/24h registered, 500 pages/month Starter, 2,500 pages/month Business. Exact page count is checked before parsing.
- **Auth + dashboard** — Flask-Login accounts, quota meter, conversion history, API key management.
- **Localisation** — English, Montenegrin/Serbian, Croatian and Bosnian; country/IP header and `Accept-Language` detection plus a navbar selector.
- **REST API v1** — `POST /api/v1/convert` with `Authorization: Bearer <key>`.
- **Security headers** — CSP, HSTS on TLS, nosniff, frame denial, no-store for account pages, capability-token downloads.

## Project layout

```text
bank-statement-converter/
├── app/
│   ├── __init__.py              # app factory, extensions, Babel, cleanup hook
│   ├── config.py                # Development / Production / Testing
│   ├── models.py                # User, UsageLog, ConversionJob, ApiKey
│   ├── services/
│   │   ├── pdf_parser.py        # multi-bank extraction + normalisation
│   │   ├── exporter.py          # formatted XLSX + UTF-8 CSV
│   │   ├── rate_limiter.py      # rolling 24h + monthly quotas
│   │   └── cleanup.py           # immediate/periodic retention sweeps
│   ├── utils/
│   │   ├── localization.py      # Balkan locale detection
│   │   ├── tokens.py            # signed expiring download tokens
│   │   └── http.py              # JSON detection + safe redirects
│   ├── blueprints/
│   │   ├── main/routes.py
│   │   ├── converter/routes.py
│   │   ├── auth/routes.py
│   │   ├── dashboard/routes.py
│   │   └── api/routes.py
│   ├── templates/
│   └── static/
├── migrations/                  # Flask-Migrate / Alembic history
├── translations/                # en, sr, hr, bs .po + compiled .mo
├── tests/test_converter.py
├── seed.py
├── run.py
├── requirements.txt
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

## Quick start (Windows / PowerShell)

The repository was developed against Python 3.12. A virtual environment is recommended.

```powershell
cd bank-statement-converter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # optional for local development
python -m flask --app run.py db upgrade
python seed.py
python run.py
```

Open <http://127.0.0.1:5000>.

If `DATABASE_URL` is unset in development, the app uses `instance/bank_statement_converter.db` (SQLite) with a warning. **Production refuses to start without an explicit PostgreSQL `DATABASE_URL` and a strong `SECRET_KEY`.**

### Demo accounts

`seed.py` is idempotent and prints the credentials and API key:

| Account | Password | Plan |
|---|---|---|
| `pro@example.com` | `demo1234` | Starter / Pro (500 pages) |
| `biz@example.com` | `demo1234` | Business / Enterprise (2,500 pages + API) |
| `free@example.com` | `demo1234` | Registered free (5 pages/24h) |

Demo API key printed by the seed:

```text
bsk_live_demo_business_8f3a1c9e7b2d4f6a
```

## PostgreSQL setup

```powershell
$env:DATABASE_URL = "postgresql://bsk_user:strong_password@127.0.0.1:5432/bank_statement_converter"
$env:SECRET_KEY = "replace-with-a-long-random-secret"
python -m flask --app run.py db upgrade
python seed.py
```

For production, also set `IP_HASH_SALT`, `API_KEY_HASH_PEPPER` and `SECRET_KEY` to separate random values. `TRUST_PROXY_HEADERS=1` only behind a trusted reverse proxy that overwrites `X-Forwarded-For`/`X-Forwarded-Proto`.

## Run tests

```powershell
python -m pytest -q
```

`tests/test_converter.py`:

- builds a one-page CKB-style PDF in memory with ReportLab,
- posts it to `/convert`,
- asserts dates, descriptions, references, debit/credit/balance and bank detection,
- downloads and inspects the XLSX and CSV,
- verifies inline edits are reflected in a second XLSX download,
- confirms the raw PDF is deleted from disk,
- confirms a second anonymous request in the same 24 hours returns **429**,
- verifies locale detection (ME → Montenegrin/Serbian, HR → Croatian, BA/BS → Bosnian),
- verifies Bearer API authentication and monthly quota charging,
- verifies a registered free account gets 5 pages and the 6th is blocked,
- verifies the line-based regex fallback on a statement with no ruled table.

Tests use in-memory SQLite and never touch the development database.

## API

### Create a key

Sign in as a Business account, open **Dashboard → API keys**, and create a key. The full key is displayed only once. Free/Starter accounts do not have API access; the API manager shows an upsell instead.

### Convert

```bash
curl -X POST "http://127.0.0.1:5000/api/v1/convert" \
  -H "Authorization: Bearer bsk_live_demo_business_8f3a1c9e7b2d4f6a" \
  -F "file=@statement.pdf"
```

Example response (abridged):

```json
{
  "success": true,
  "api_version": "v1",
  "job_id": 42,
  "token": "signed-token",
  "page_count": 1,
  "row_count": 3,
  "detected_bank": "CKB",
  "rows": [
    {
      "date": "2024-02-01",
      "description": "Grocery Market",
      "reference": "REF-001",
      "debit": 1250.5,
      "credit": null,
      "balance": 15000.0
    }
  ],
  "downloads": {
    "xlsx": "http://127.0.0.1:5000/convert/download/<token>/xlsx",
    "csv": "http://127.0.0.1:5000/convert/download/<token>/csv"
  },
  "quota": { "scope": "paid", "limit": 2500, "used": 1, "remaining": 2499 }
}
```

Errors use `{"success": false, "error": "code", "message": "..."}` and appropriate status codes (`400`, `401`, `403`, `413`, `422`, `429`, `500`). `429` includes `Retry-After` and a `quota` object.

## Quotas and retention

- Anonymous: sum of page counts from the same hashed IP in the last rolling 24 hours; limit 1.
- Registered free: sum of page counts for the user in the last rolling 24 hours; limit 5.
- Paid: monthly `pages_used_this_cycle` against `monthly_page_quota`; cycle resets automatically.
- Page count comes from `pypdf` and is checked **before** parsing. Failed parses do not consume quota.
- Uploaded PDFs are removed immediately after extraction (including on failure). A periodic before-request cleanup also removes stale files and clears `parsed_data_json` after `CONVERSION_RETENTION_SECONDS` (default 3,600).

## Localisation

Catalogues are in `translations/{en,sr,hr,bs}/LC_MESSAGES/messages.po` and are compiled to `.mo`.

Detection order:

1. `?lang=` choice (saved in session),
2. session preference,
3. signed-in user preference,
4. country header (`CF-IPCountry`, `X-Country-Code`, `X-Vercel-IP-Country`, …): `ME`/`RS` → `sr`, `HR` → `hr`, `BA` → `bs`,
5. `Accept-Language`,
6. English.

To regenerate:

```powershell
pybabel extract -F babel.cfg -o messages.pot .
pybabel update -i messages.pot -d translations
pybabel compile -d translations
```

## Docker / production deployment

```powershell
$env:SECRET_KEY = "replace-with-a-long-random-secret"
$env:POSTGRES_PASSWORD = "replace-with-a-strong-password"
docker compose up --build
```

The app listens on <http://127.0.0.1:8000>. Compose runs `flask --app run.py db upgrade` before starting Gunicorn. The upload volume is ephemeral by design; only in-progress files exist there. The compose file sets `SESSION_COOKIE_SECURE=0` because it serves plain HTTP locally — remove that override (the production default is `1`) when you put TLS in front of the app.

For non-Docker production:

```bash
pip install -r requirements.txt
export FLASK_ENV=production
export DATABASE_URL=postgresql://...
export SECRET_KEY=...
flask --app run.py db upgrade
gunicorn --bind 0.0.0.0:8000 --workers 3 --timeout 180 run:app
```

Put TLS and a reverse proxy in front of Gunicorn, set `TRUST_PROXY_HEADERS=1`, and keep `SESSION_COOKIE_SECURE` enabled (the production config already does).

## Environment variables

See `.env.example`. Key variables:

| Variable | Purpose |
|---|---|
| `FLASK_ENV` | `development`, `production`, `testing` |
| `SECRET_KEY` | session signing; required and strong in production |
| `DATABASE_URL` | PostgreSQL DSN (production requires it) |
| `UPLOAD_FOLDER` | private temporary upload directory |
| `MAX_CONTENT_LENGTH_MB` | upload limit, default 25 MB |
| `CONVERSION_RETENTION_SECONDS` | parsed preview retention, default 3,600 |
| `MAX_PDF_PAGES` | per-conversion page limit, default 500 |
| `TRUST_PROXY_HEADERS` | trust `X-Forwarded-*` only behind a trusted proxy |
| `SESSION_COOKIE_SECURE` | production defaults to `1`; set `0` only for local HTTP demos |
| `IP_HASH_SALT` | salt for usage-log IP hashes |
| `API_KEY_HASH_PEPPER` | pepper for API key hashes |
| `SUPPORT_EMAIL`, `SALES_EMAIL` | public contact addresses |

## Scope notes

- The converter handles text-based PDFs. Scanned image statements require OCR and return a clear error.
- Billing/checkout provider integration is intentionally not hard-coded because no processor was specified. Pricing CTAs route to lead capture; plan tiers can be provisioned by an admin/billing workflow using `User.set_plan()`.
- Batch multi-PDF conversion is presented as a paid-plan capability; the current UI uploads and previews one statement at a time.
- `pandas` is included in the stack for downstream analysis; export generation uses `openpyxl`/`csv` directly to keep workbook formatting deterministic.

## License

This implementation is provided for the requesting project. Add your own license before commercial redistribution.
