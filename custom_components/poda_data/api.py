"""Lightweight client for the klient.poda.cz customer portal.

The portal (https://klient.poda.cz) is a Yii2 application. Login is
protected by a CSRF token that Yii2 embeds both as a hidden form field
and as a signed cookie. Rather than hard-coding field names (which can
change between PODA releases), this client parses the actual login
<form> from the HTML and resubmits every field it finds, only
overwriting whichever fields look like the username/password inputs.
This makes the client considerably more resilient to small markup
changes than hard-coded field names would be.

After login, the client fetches the "Vyúčtování" (billing) page for
mobile numbers and:
  * scrapes the "Datové přenosy" (data usage) table directly from the
    HTML to get the subscriber name and the FUP limit ("Objem"), and
  * follows the "Stáhnout jako CSV" links for the "Volání" (calls),
    "SMS a MMS" and "Datové přenosy" (data) sections and downloads +
    parses those three CSV exports.

Confirmed CSV column layouts (verified against real exports):
  * Calls: src, dst, start, billsec, price, free_units
  * SMS:   src, dst, type, start, price, free_units
  * Data:  src, start, kb, price, zone  (one row per data session,
           usage in kilobytes; summed per number and converted to MB)
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from .const import BASE_URL, BILLING_URL, DEFAULT_HEADERS, LOGIN_URL

_LOGGER = logging.getLogger(__name__)


class PodaAuthError(Exception):
    """Raised when login fails (bad credentials or unexpected form)."""


class PodaConnectionError(Exception):
    """Raised on network / unexpected response errors."""


@dataclass
class NumberStats:
    """Aggregated statistics for a single mobile number."""

    number: str
    name: str | None = None
    calls_seconds: int = 0
    calls_price: float = 0.0
    calls_count: int = 0
    sms_count: int = 0
    sms_price: float = 0.0
    data_used_mb: float | None = None
    data_limit_mb: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def calls_minutes(self) -> float:
        return round(self.calls_seconds / 60, 2)


def _normalize_number(raw: str) -> str:
    """Normalize a phone number to the local 9-digit CZ format.

    The CSV export uses the full international format, e.g.
    '00420734714008', while the billing page displays '734714008'.
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00420"):
        digits = digits[5:]
    elif digits.startswith("420") and len(digits) > 9:
        digits = digits[3:]
    return digits


def _parse_number(text: str) -> float:
    """Parse a Czech-formatted number ('1 234,50') into a float."""
    if text is None:
        return 0.0
    cleaned = text.replace("\xa0", "").replace(" ", "").replace(",", ".")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


class PodaClient:
    """Handles authentication and data retrieval from klient.poda.cz."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._logged_in = False

    async def async_login(self) -> None:
        """Log in to the portal, following the real login form."""
        try:
            async with self._session.get(LOGIN_URL, headers=DEFAULT_HEADERS) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise PodaConnectionError(f"Could not load login page: {err}") from err

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id=re.compile("login", re.I)) or soup.find("form")
        if form is None:
            raise PodaAuthError("Login form not found on login page")

        action = form.get("action") or LOGIN_URL
        post_url = urljoin(LOGIN_URL, action)

        payload: dict[str, str] = {}
        for inp in form.find_all(["input", "select"]):
            name = inp.get("name")
            if not name:
                continue
            payload[name] = inp.get("value", "")

        # Best-effort detection of the username/password fields.
        user_field = pass_field = None
        for name in payload:
            lname = name.lower()
            if pass_field is None and "pass" in lname:
                pass_field = name
            elif user_field is None and (
                "user" in lname or "login" in lname or "email" in lname
            ):
                user_field = name

        if user_field is None or pass_field is None:
            # Fallback to the common Yii2 "advanced app" naming scheme.
            user_field = user_field or "LoginForm[username]"
            pass_field = pass_field or "LoginForm[password]"

        payload[user_field] = self._username
        payload[pass_field] = self._password

        headers = dict(DEFAULT_HEADERS)
        headers["Referer"] = LOGIN_URL

        try:
            async with self._session.post(
                post_url, data=payload, headers=headers, allow_redirects=True
            ) as resp:
                resp.raise_for_status()
                final_url = str(resp.url)
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise PodaConnectionError(f"Login request failed: {err}") from err

        if "login" in final_url.lower() and (
            "heslo" in html.lower() or "password" in html.lower()
        ):
            # Still on a login-looking page -> credentials likely rejected.
            if re.search(r"neplatn|nespr\u00e1vn|invalid", html, re.I):
                raise PodaAuthError("Login rejected - check username/password")

        self._logged_in = True

    async def async_get_stats(self) -> dict[str, NumberStats]:
        """Fetch and parse the current-month billing page for all numbers."""
        if not self._logged_in:
            await self.async_login()

        html = await self._fetch(BILLING_URL)
        soup = BeautifulSoup(html, "html.parser")

        if soup.find("form", id=re.compile("login", re.I)):
            # Session expired mid-way, try one re-login.
            self._logged_in = False
            await self.async_login()
            html = await self._fetch(BILLING_URL)
            soup = BeautifulSoup(html, "html.parser")

        stats: dict[str, NumberStats] = {}

        # HTML table gives us the name + the FUP limit ("Objem"), and also
        # a fallback "used" value in case the data CSV export ever changes.
        self._parse_data_usage(soup, stats)

        calls_url = self._find_csv_link(soup, "Vol\u00e1n\u00ed")
        if calls_url:
            csv_bytes = await self._fetch_bytes(calls_url)
            self._parse_calls_csv(csv_bytes, stats)
        else:
            _LOGGER.warning("Could not find CSV export link for 'Volání' section")

        sms_url = self._find_csv_link(soup, "SMS")
        if sms_url:
            csv_bytes = await self._fetch_bytes(sms_url)
            self._parse_sms_csv(csv_bytes, stats)
        else:
            _LOGGER.warning("Could not find CSV export link for 'SMS a MMS' section")

        data_url = self._find_csv_link(soup, "Datov\u00e9")
        if data_url:
            csv_bytes = await self._fetch_bytes(data_url)
            # CSV export is per-session and more precise than the rounded
            # HTML table value, so it overrides the fallback set above.
            self._parse_data_csv(csv_bytes, stats)
        else:
            _LOGGER.debug(
                "No CSV export link found for 'Datové přenosy' - using HTML table value"
            )

        return stats

    async def _fetch(self, url: str) -> str:
        try:
            async with self._session.get(url, headers=DEFAULT_HEADERS) as resp:
                resp.raise_for_status()
                return await resp.text()
        except aiohttp.ClientError as err:
            raise PodaConnectionError(f"GET {url} failed: {err}") from err

    async def _fetch_bytes(self, url: str) -> bytes:
        try:
            async with self._session.get(url, headers=DEFAULT_HEADERS) as resp:
                resp.raise_for_status()
                return await resp.read()
        except aiohttp.ClientError as err:
            raise PodaConnectionError(f"GET {url} failed: {err}") from err

    @staticmethod
    def _find_csv_link(soup: BeautifulSoup, section_heading: str) -> str | None:
        """Find the 'Stáhnout jako CSV' link belonging to a given section."""
        heading = soup.find(
            lambda tag: tag.name in ("h1", "h2", "h3", "h4")
            and section_heading.lower() in tag.get_text(strip=True).lower()
        )
        if heading is None:
            return None

        node = heading
        for _ in range(30):
            node = node.find_next(["a", "h1", "h2", "h3", "h4"])
            if node is None:
                break
            if node.name in ("h1", "h2", "h3", "h4"):
                break
            if node.name == "a" and "csv" in node.get_text(strip=True).lower():
                href = node.get("href")
                if href:
                    return urljoin(BASE_URL, href)
        return None

    @staticmethod
    def _parse_data_usage(soup: BeautifulSoup, stats: dict[str, NumberStats]) -> None:
        heading = soup.find(
            lambda tag: tag.name in ("h1", "h2", "h3", "h4")
            and "datov" in tag.get_text(strip=True).lower()
        )
        if heading is None:
            _LOGGER.warning("Could not find 'Datové přenosy' section")
            return

        table = heading.find_next("table")
        if table is None:
            return

        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]

        def col_index(*keywords: str) -> int | None:
            for i, h in enumerate(headers):
                if any(k in h for k in keywords):
                    return i
            return None

        idx_number = col_index("\u010d\u00edslo")
        idx_name = col_index("n\u00e1zev")
        idx_volume = col_index("objem")
        idx_used = col_index("vy\u010derp\u00e1no")

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells or idx_number is None or idx_number >= len(cells):
                continue
            raw_number = cells[idx_number].get_text(strip=True)
            number = _normalize_number(raw_number)
            if not number:
                continue

            entry = stats.setdefault(number, NumberStats(number=number))
            if idx_name is not None and idx_name < len(cells):
                entry.name = cells[idx_name].get_text(strip=True) or entry.name
            if idx_volume is not None and idx_volume < len(cells):
                entry.data_limit_mb = _parse_data_volume(cells[idx_volume].get_text(strip=True))
            if idx_used is not None and idx_used < len(cells):
                entry.data_used_mb = _parse_data_volume(cells[idx_used].get_text(strip=True))

    @staticmethod
    def _parse_calls_csv(csv_bytes: bytes, stats: dict[str, NumberStats]) -> None:
        text = csv_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [f.lower() for f in (reader.fieldnames or [])]

        src_field = _find_field(fieldnames, reader.fieldnames, ["src", "cislo", "volajici"])
        dur_field = _find_field(fieldnames, reader.fieldnames, ["billsec", "delka", "duration"])
        price_field = _find_field(fieldnames, reader.fieldnames, ["price", "cena"])

        if src_field is None:
            _LOGGER.warning("Calls CSV: could not identify the source-number column")
            return

        for row in reader:
            number = _normalize_number(row.get(src_field, ""))
            if not number:
                continue
            entry = stats.setdefault(number, NumberStats(number=number))
            entry.calls_count += 1
            if dur_field:
                entry.calls_seconds += int(_parse_number(row.get(dur_field, "0")))
            if price_field:
                entry.calls_price += _parse_number(row.get(price_field, "0"))

    @staticmethod
    def _parse_data_csv(csv_bytes: bytes, stats: dict[str, NumberStats]) -> None:
        """Parse the data-usage CSV export.

        Confirmed columns: src, start, kb, price, zone
        One row per data session; usage is in kilobytes (base 1024).
        """
        text = csv_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [f.lower() for f in (reader.fieldnames or [])]

        src_field = _find_field(fieldnames, reader.fieldnames, ["src", "cislo"])
        kb_field = _find_field(fieldnames, reader.fieldnames, ["kb", "objem", "data"])
        price_field = _find_field(fieldnames, reader.fieldnames, ["price", "cena"])

        if src_field is None or kb_field is None:
            _LOGGER.warning(
                "Data CSV: could not identify the number/kb columns, keeping HTML table value"
            )
            return

        used_kb: dict[str, float] = {}
        price_sum: dict[str, float] = {}
        for row in reader:
            number = _normalize_number(row.get(src_field, ""))
            if not number:
                continue
            used_kb[number] = used_kb.get(number, 0.0) + _parse_number(row.get(kb_field, "0"))
            if price_field:
                price_sum[number] = price_sum.get(number, 0.0) + _parse_number(
                    row.get(price_field, "0")
                )

        for number, kb in used_kb.items():
            entry = stats.setdefault(number, NumberStats(number=number))
            entry.data_used_mb = round(kb / 1024, 2)
            entry.raw["data_price"] = price_sum.get(number, 0.0)

    @staticmethod
    def _parse_sms_csv(csv_bytes: bytes, stats: dict[str, NumberStats]) -> None:
        text = csv_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [f.lower() for f in (reader.fieldnames or [])]

        src_field = _find_field(fieldnames, reader.fieldnames, ["src", "cislo", "odesilatel"])
        price_field = _find_field(fieldnames, reader.fieldnames, ["price", "cena"])

        if src_field is None:
            _LOGGER.warning("SMS CSV: could not identify the source-number column")
            return

        for row in reader:
            number = _normalize_number(row.get(src_field, ""))
            if not number:
                continue
            entry = stats.setdefault(number, NumberStats(number=number))
            entry.sms_count += 1
            if price_field:
                entry.sms_price += _parse_number(row.get(price_field, "0"))


def _find_field(lower_fieldnames: list[str], original: list[str] | None, keywords: list[str]) -> str | None:
    if not original:
        return None
    for i, name in enumerate(lower_fieldnames):
        if any(k in name for k in keywords):
            return original[i]
    return None


def _parse_data_volume(text: str) -> float:
    """Parse a value like '3500 MB' or '1,2 GB' into megabytes."""
    if not text:
        return 0.0
    value = _parse_number(text)
    if "gb" in text.lower():
        value *= 1024
    return value
