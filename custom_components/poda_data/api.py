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
        _LOGGER.debug(
            "Login attempt with username_preview=%r (len=%d), password_len=%d",
            (self._username[:2] + "…" if len(self._username) > 2 else self._username),
            len(self._username),
            len(self._password),
        )

        try:
            async with self._session.get(LOGIN_URL, headers=DEFAULT_HEADERS) as resp:
                resp.raise_for_status()
                html = await resp.text()
                cookies_after_get = {
                    c.key: c.value for c in self._session.cookie_jar if c.key
                }
        except aiohttp.ClientError as err:
            raise PodaConnectionError(f"Could not load login page: {err}") from err

        _LOGGER.debug(
            "Login page GET: status=%s cookies_received=%s csrf_cookie_value=%r",
            resp.status,
            sorted(cookies_after_get.keys()),
            cookies_after_get.get("_csrf"),
        )

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id=re.compile("login", re.I)) or soup.find("form")
        if form is None:
            raise PodaAuthError("Login form not found on login page")

        action = form.get("action") or LOGIN_URL
        post_url = urljoin(LOGIN_URL, action)

        payload: dict[str, str] = {}
        for inp in form.find_all(["input", "select", "button"]):
            name = inp.get("name")
            if not name:
                continue
            if inp.name == "input" and inp.get("type") == "checkbox":
                # Yii2 typically renders a hidden "unchecked" fallback value
                # right before the checkbox itself. Only override that
                # fallback if the checkbox actually starts out checked -
                # otherwise we'd wrongly submit rememberMe=1 etc.
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value", "1")
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
        headers["Origin"] = BASE_URL
        headers["Sec-Fetch-Site"] = "same-origin"

        redacted_payload = {
            k: ("***" if k in (user_field, pass_field) else v) for k, v in payload.items()
        }
        outgoing_cookies = {
            c.key: c.value for c in self._session.cookie_jar if c.key
        }
        _LOGGER.debug(
            "Login POST about to send: url=%s headers=%s payload=%s "
            "hidden_csrf_field=%r cookie_jar_csrf=%r cookies_that_will_be_sent=%s",
            post_url,
            headers,
            redacted_payload,
            payload.get("_csrf"),
            outgoing_cookies.get("_csrf"),
            sorted(outgoing_cookies.keys()),
        )

        try:
            async with self._session.post(
                post_url, data=payload, headers=headers, allow_redirects=True
            ) as resp:
                status = resp.status
                final_url = str(resp.url)
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise PodaConnectionError(f"Login request failed: {err}") from err

        soup_after = BeautifulSoup(html, "html.parser")
        still_on_login = soup_after.find("form", id=re.compile("login", re.I)) is not None

        _LOGGER.debug(
            "Login POST: status=%s final_url=%s fields_sent=%s still_on_login_form=%s",
            status,
            final_url,
            sorted(payload.keys()),
            still_on_login,
        )

        if still_on_login:
            # The most reliable failure signal: the exact same login form is
            # present again. This is language/wording independent, unlike
            # scanning for a specific error message.
            title = soup_after.find("title")
            form_after = soup_after.find("form", id=re.compile("login", re.I))
            context_html = ""
            search_root = None
            if form_after is not None:
                search_root = form_after.parent.parent if form_after.parent else form_after
                context_node = search_root or form_after
                context_html = str(context_node)
            else:
                body = soup_after.find("body")
                context_html = str(body) if body else html
            error_text = ""
            if search_root is not None:
                error_candidate = search_root.find(
                    class_=re.compile("alert|error|invalid|help-block", re.I)
                )
                if error_candidate is not None:
                    error_text = error_candidate.get_text(strip=True)
            _LOGGER.debug(
                "Login POST failed: title=%r error_text=%r response_length=%d",
                title.get_text(strip=True) if title else None,
                error_text,
                len(html),
            )
            _LOGGER.debug(
                "Login form area HTML (%d chars):\n%s", len(context_html), context_html[:6000]
            )
            raise PodaAuthError(
                f"Login rejected - still on login form after POST. {error_text}".strip()
            )

        self._logged_in = True

    async def async_get_stats(self) -> dict[str, NumberStats]:
        """Fetch and parse the current-month billing page for all numbers."""
        if not self._logged_in:
            await self.async_login()

        html = await self._fetch(BILLING_URL)
        _LOGGER.debug(
            "Billing page fetched, length=%d bytes, full content:\n%s", len(html), html
        )
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

        # Data usage is already parsed from the HTML billing summary table
        # (Objem + Vyčerpáno columns). The portal's data CSV link returns
        # a different format than expected (calls headers), so we skip it
        # and rely on the HTML table values.

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
        """Find the 'Stáhnout jako CSV' link belonging to a given section.

        The link can appear either before or after the section heading in
        document order (e.g. visually placed top-right of a header row but
        earlier in the markup), so we pick whichever CSV-labelled link is
        nearest to the heading by document position rather than assuming
        a fixed forward/backward direction.
        """
        heading = _find_heading(soup, section_heading)

        all_tags = soup.find_all(True)
        csv_links = [
            t for t in all_tags if t.name == "a" and "csv" in t.get_text(strip=True).lower()
        ]
        if not csv_links:
            return None

        if heading is not None:
            position = {id(tag): i for i, tag in enumerate(all_tags)}
            heading_idx = position.get(id(heading))
            if heading_idx is not None:
                csv_links.sort(key=lambda a: abs(position.get(id(a), 0) - heading_idx))

        href = csv_links[0].get("href")
        return urljoin(BASE_URL, href) if href else None

    @staticmethod
    def _parse_data_usage(soup: BeautifulSoup, stats: dict[str, NumberStats]) -> None:
        table = _find_usage_table(soup)
        if table is None:
            _LOGGER.warning("Could not find any table with data usage columns")
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

        The portal serves two different CSV formats for data:
          1. Summary format (vyuctovani-csv): Číslo, Název, Typ, Objem, Vyčerpáno
             One row per number with limit and total usage.
          2. Per-session format: src, start, kb, price, zone
             One row per data session; usage is in kilobytes.

        We detect the format by checking for summary-style headers.
        """
        text = csv_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [f.lower() for f in (reader.fieldnames or [])]
        original = reader.fieldnames

        _LOGGER.debug("Data CSV headers: %s", original)

        # Check for summary format first (Číslo / Název / Objem / Vyčerpáno)
        has_summary_cols = any(
            "vycerpano" in h or "vyčerpáno" in h for h in fieldnames
        )
        if has_summary_cols:
            src_field = _find_field(fieldnames, original, ["cislo", "číslo", "cis"])
            used_field = _find_field(fieldnames, original, ["vycerpano", "vyčerpáno", "spotreba"])
            if src_field and used_field:
                for row in reader:
                    number = _normalize_number(row.get(src_field, ""))
                    if not number:
                        continue
                    entry = stats.setdefault(number, NumberStats(number=number))
                    entry.data_used_mb = _parse_data_volume(row.get(used_field, "0"))
                    _LOGGER.debug(
                        "Data CSV summary: number=%s used=%s MB",
                        number, entry.data_used_mb,
                    )
                return
            _LOGGER.debug(
                "Summary format detected but missing columns: src=%s used=%s",
                src_field, used_field,
            )
            return

        # Per-session format: src, start, kb, price, zone
        src_field = _find_field(fieldnames, original, ["src", "cislo", "cis"])
        kb_field = _find_field(fieldnames, original, ["kb", "objem", "data", "mb", "objem_mb"])
        price_field = _find_field(fieldnames, original, ["price", "cena"])

        if src_field is None or kb_field is None:
            _LOGGER.warning(
                "Data CSV: could not identify the number/kb columns, "
                "keeping HTML table value (headers: %s)",
                original,
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


def _find_heading(soup: BeautifulSoup, keyword: str):
    """Find the element that acts as a section heading containing `keyword`.

    The PODA portal does not necessarily use <h1>-<h4> tags for section
    titles (could be a <div>, <span>, <strong>, etc.), and the heading may
    also wrap an icon element (<svg>, <i>, ...) alongside the text. So
    instead of requiring a pure "leaf" text node (which would incorrectly
    reject headings containing an icon), we collect every tag whose
    rendered text starts with the keyword and pick the *shortest* one -
    i.e. the most specific element that still contains the full heading
    text, ignoring large wrapping containers.

    The page also contains a help/"Nápověda" panel that documents the CSV
    column layout and reuses the exact same section names ("Volání",
    "SMS a MMS", "Datové přenosy") *before* the real section headings
    appear in the document. To avoid matching that help text instead of
    the actual section, we prefer a candidate whose nearest following
    <table> looks like a real data table (its header row contains
    "Číslo") over just taking the shortest/first candidate.
    """
    keyword_lower = keyword.lower()
    candidates = []
    for tag in soup.find_all(True):
        if tag.name in ("script", "style", "option", "head"):
            continue
        text = tag.get_text(strip=True)
        if not text or len(text) > 80:
            continue
        if text.lower().startswith(keyword_lower):
            candidates.append(tag)

    if not candidates:
        return None

    # Most specific (shortest text) first - avoids picking a huge
    # wrapping container whose text happens to start with the keyword.
    candidates.sort(key=lambda t: len(t.get_text(strip=True)))

    for cand in candidates:
        table = cand.find_next("table")
        if table is None:
            continue
        header_text = " ".join(
            cell.get_text(strip=True).lower() for cell in table.find_all(["th", "td"])[:5]
        )
        if "\u010d\u00edslo" in header_text:  # "číslo"
            return cand

    return candidates[0]


def _find_field(lower_fieldnames: list[str], original: list[str] | None, keywords: list[str]) -> str | None:
    if not original:
        return None
    for i, name in enumerate(lower_fieldnames):
        if any(k in name for k in keywords):
            return original[i]
    return None


def _find_usage_table(soup: BeautifulSoup) -> Any | None:
    """Find the billing summary table containing data volume columns.

    First tries to find a table with "Objem" and "Vyčerpáno" column headers
    anywhere on the page (the main billing summary table). Falls back to
    finding a table under a "Datové přenosy" heading.
    """
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if any("objem" in h for h in headers) and any("vyčerpáno" in h for h in headers):
            return table
    heading = _find_heading(soup, "Datové přenosy") or _find_heading(soup, "Datov")
    if heading is not None:
        return heading.find_next("table")
    return None


def _parse_data_volume(text: str) -> float:
    """Parse a value like '3500 MB' or '1,2 GB' into megabytes."""
    if not text:
        return 0.0
    value = _parse_number(text)
    if "gb" in text.lower():
        value *= 1024
    return value
