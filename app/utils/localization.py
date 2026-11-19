"""Locale detection for English and the main Balkan languages.

Detection priority:
1. explicit ``?lang=`` choice (persisted in the session),
2. saved session preference,
3. signed-in user's saved preference,
4. CDN / reverse-proxy country header (ME, RS, HR, BA),
5. ``Accept-Language``,
6. English.
"""

from __future__ import annotations

from flask import request, session
from flask_login import current_user

SUPPORTED_LOCALES = ("en", "sr", "hr", "bs")

LOCALE_LABELS = {
    "en": "English",
    "sr": "Crnogorski / Srpski",
    "hr": "Hrvatski",
    "bs": "Bosanski",
}

LOCALE_SHORT_LABELS = {"en": "EN", "sr": "ME/SR", "hr": "HR", "bs": "BS"}

COUNTRY_LOCALE_MAP = {
    "ME": "sr",
    "RS": "sr",
    "HR": "hr",
    "BA": "bs",
}

_COUNTRY_HEADERS = (
    "CF-IPCountry",
    "X-Country-Code",
    "X-Vercel-IP-Country",
    "X-Geo-Country",
    "X-AppEngine-Country",
)

_ACCEPT_LANGUAGE_ALIASES = {
    "sr": "sr",
    "sr-rs": "sr",
    "sr-latn": "sr",
    "sr-latn-rs": "sr",
    "cnr": "sr",
    "me": "sr",
    "hr": "hr",
    "hr-hr": "hr",
    "bs": "bs",
    "bs-ba": "bs",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
}


def normalize_locale(value: str | None) -> str | None:
    """Return a supported locale code for a language tag, if one matches."""
    if not value:
        return None
    tag = value.strip().replace("_", "-").lower()
    if tag in SUPPORTED_LOCALES:
        return tag
    if tag in _ACCEPT_LANGUAGE_ALIASES:
        return _ACCEPT_LANGUAGE_ALIASES[tag]
    primary = tag.split("-", 1)[0]
    return primary if primary in SUPPORTED_LOCALES else None


def country_from_request() -> str | None:
    for header in _COUNTRY_HEADERS:
        value = (request.headers.get(header) or "").strip().upper()
        if len(value) == 2 and value.isalpha():
            return value
    return None


def locale_from_accept_language() -> str | None:
    languages = request.accept_languages
    for lang, _quality in languages:
        normalised = normalize_locale(lang)
        if normalised:
            return normalised
    return None


def select_locale() -> str:
    query_lang = normalize_locale(request.args.get("lang"))
    if query_lang:
        session["lang"] = query_lang
        return query_lang

    session_lang = normalize_locale(session.get("lang"))
    if session_lang:
        return session_lang

    if getattr(current_user, "is_authenticated", False):
        preferred = normalize_locale(getattr(current_user, "preferred_lang", None))
        if preferred:
            return preferred

    country = country_from_request()
    if country and country in COUNTRY_LOCALE_MAP:
        return COUNTRY_LOCALE_MAP[country]

    return locale_from_accept_language() or "en"


def remember_language(locale: str) -> None:
    normalised = normalize_locale(locale)
    if normalised:
        session["lang"] = normalised
