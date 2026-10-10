"""Lightweight client for the klient.poda.cz customer portal.

The portal (https://klient.poda.cz) is a Yii2 application. Login is
protected by a CSRF token that Yii2 embeds both as a hidden form field
and as a signed cookie. Rather than hard-coding field names (which can
change between PODA releases), this client parses the actual login
<form> from the HTML and resubmits every field it finds, only
overwriting whichever fields look like the username/password inputs.
This makes the client considerably more resilient to small markup
changes than hard-coded field names would be.

After login, the client fetches the "VyĂşÄŤtovĂˇnĂ­" (billing) page for
mobile numbers and:
  * scrapes the "DatovĂ© pĹ™enosy" (data usage) table directly from the
    HTML to get the subscriber name and the FUP limit ("Objem"), and
  * follows the "StĂˇhnout jako CSV" links for the "VolĂˇnĂ­" (calls),
    "SMS a MMS" and "DatovĂ© pĹ™enosy" (data) sections and downloads +
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
from urllib.parse import urljoin, urlsplit

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
    '00420000000001', while the billing page displays '000000001'.
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00420"):
        digits = digits[5:]
    elif digits.startswith("420") and len(digits) > 9:
        digits = digits[3:]
    return digits if re.fullmatch(r"[0-9]{9}", digits) else ""


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


def _portal_url(base: str, target: str) -> str:
    """Keep credentials and authenticated downloads on the HTTPS portal."""
    url = urljoin(base, target)
    parsed = urlsplit(url)
    expected = urlsplit(BASE_URL)
    if (parsed.scheme != "https" or parsed.hostname != expected.hostname
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise PodaConnectionError("Portal returned an untrusted URL")
    return url


class PodaClient:
    """Handles authentication and data retrieval from klient.poda.cz."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._logged_in = False

    async def async_login(self) -> None:
        """Log in to the portal, following the real login form."""
        html = await self._fetch(LOGIN_URL)

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id=re.compile("login", re.I)) or soup.find("form")
        if form is None:
            raise PodaAuthError("Login form not found on login page")

        action = form.get("action") or LOGIN_URL
        post_url = _portal_url(LOGIN_URL, action)

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

        try:
            async with self._session.post(
                post_url, data=payload, headers=headers, allow_redirects=False
            ) as resp:
                status = resp.status
                location = resp.headers.get("Location")
                html = await resp.text()
        except (aiohttp.ClientError, TimeoutError):
            raise PodaConnectionError("Login request failed") from None

        if 300 <= status < 400:
            if status not in (301, 302, 303) or not location:
                raise PodaConnectionError("Unsupported login redirect")
            html = await self._fetch(_portal_url(post_url, location))
            status = 200

        soup_after = BeautifulSoup(html, "html.parser")
        still_on_login = soup_after.find("form", id=re.compile("login", re.I)) is not None

        if status in (401, 403) or still_on_login:
            raise PodaAuthError("Login rejected by portal")
        if status >= 400:
            raise PodaConnectionError("Login request failed")
        _LOGGER.debug("Portal login completed")

        self._logged_in = True

    async def async_get_stats(self) -> dict[str, NumberStats]:
        """Fetch and parse the current-month billing page for all numbers."""
        if not self._logged_in:
            await self.async_login()

        html = await self._fetch(BILLING_URL)
        _LOGGER.debug("Billing page fetched, length=%d bytes", len(html))
        soup = BeautifulSoup(html, "html.parser")

        if soup.find("form", id=re.compile("login", re.I)):
            # Session expired mid-way, try one re-login.
            self._logged_in = False
            await self.async_login()
            html = await self._fetch(BILLING_URL)
            soup = BeautifulSoup(html, "html.parser")

        if soup.find("form", id=re.compile("login", re.I)):
            self._logged_in = False
            raise PodaAuthError("Portal session could not be restored")

        stats: dict[str, NumberStats] = {}
        self._discover_numbers(soup, stats)

        # HTML table gives us the name + the FUP limit ("Objem"), and also
        # a fallback "used" value in case the data CSV export ever changes.
        self._parse_data_usage(soup, stats)

        calls_url = self._find_csv_link(soup, "Vol\u00e1n\u00ed")
        if calls_url:
            csv_bytes = await self._fetch_bytes(calls_url)
            self._parse_calls_csv(csv_bytes, stats)
        else:
            _LOGGER.warning("Could not find CSV export link for 'VolĂˇnĂ­' section")

        sms_url = self._find_csv_link(soup, "SMS")
        if sms_url:
            csv_bytes = await self._fetch_bytes(sms_url)
            self._parse_sms_csv(csv_bytes, stats)
        else:
            _LOGGER.warning("Could not find CSV export link for 'SMS a MMS' section")

        # Data usage is already parsed from the HTML billing summary table
        # (Objem + VyÄŤerpĂˇno columns). The portal's data CSV link returns
        # a different format than expected (calls headers), so we skip it
        # and rely on the HTML table values.

        _LOGGER.debug("Billing statistics updated for %d numbers", len(stats))

        return stats

    async def _fetch(self, url: str) -> str:
        return (await self._request_get(url, binary=False))

    async def _fetch_bytes(self, url: str) -> bytes:
        return (await self._request_get(url, binary=True))

    async def _request_get(self, url: str, *, binary: bool) -> str | bytes:
        """Follow a bounded number of redirects only inside the portal."""
        url = _portal_url(BASE_URL, url)
        try:
            for _ in range(6):
                async with self._session.get(
                    url, headers=DEFAULT_HEADERS, allow_redirects=False
                ) as resp:
                    if 300 <= resp.status < 400:
                        location = resp.headers.get("Location")
                        if not location:
                            raise PodaConnectionError("Portal redirect has no location")
                        url = _portal_url(url, location)
                        continue
                    resp.raise_for_status()
                    return await resp.read() if binary else await resp.text()
        except (aiohttp.ClientError, TimeoutError):
            raise PodaConnectionError("Portal request failed") from None
        raise PodaConnectionError("Too many portal redirects")

    @staticmethod
    def _find_csv_link(soup: BeautifulSoup, section_heading: str) -> str | None:
        """Find the 'StĂˇhnout jako CSV' link belonging to a given section.

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
        return _portal_url(BASE_URL, href) if href else None

    @staticmethod
    def _discover_numbers(soup: BeautifulSoup, stats: dict[str, NumberStats]) -> None:
        """Discover owned numbers from billing number columns, even with no usage."""
        for table in soup.find_all("table"):
            headers = [cell.get_text(" ", strip=True).lower() for cell in table.find_all("th")]
            index = next(
                (i for i, header in enumerate(headers) if header in
                 ("ÄŤĂ­slo", "cislo", "telefonnĂ­ ÄŤĂ­slo", "telefonni cislo")),
                None,
            )
            if index is None:
                continue
            for row in table.find_all("tr"):
                cells = row.find_all("td", recursive=False)
                if index >= len(cells):
                    continue
                number = _normalize_number(cells[index].get_text(strip=True))
                if number:
                    stats.setdefault(number, NumberStats(number=number))

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
          1. Summary format (vyuctovani-csv): ÄŚĂ­slo, NĂˇzev, Typ, Objem, VyÄŤerpĂˇno
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

        # Check for summary format first (ÄŚĂ­slo / NĂˇzev / Objem / VyÄŤerpĂˇno)
        has_summary_cols = any(
            "vycerpano" in h or "vyÄŤerpĂˇno" in h for h in fieldnames
        )
        if has_summary_cols:
            src_field = _find_field(fieldnames, original, ["cislo", "ÄŤĂ­slo", "cis"])
            used_field = _find_field(fieldnames, original, ["vycerpano", "vyÄŤerpĂˇno", "spotreba"])
            if src_field and used_field:
                for row in reader:
                    number = _normalize_number(row.get(src_field, ""))
                    if not number:
                        continue
                    entry = stats.setdefault(number, NumberStats(number=number))
                    entry.data_used_mb = _parse_data_volume(row.get(used_field, "0"))
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

    The page also contains a help/"NĂˇpovÄ›da" panel that documents the CSV
    column layout and reuses the exact same section names ("VolĂˇnĂ­",
    "SMS a MMS", "DatovĂ© pĹ™enosy") *before* the real section headings
    appear in the document. To avoid matching that help text instead of
    the actual section, we prefer a candidate whose nearest following
    <table> looks like a real data table (its header row contains
    "ÄŚĂ­slo") over just taking the shortest/first candidate.
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
        if "\u010d\u00edslo" in header_text:  # "ÄŤĂ­slo"
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

    First tries to find a table with "Objem" and "VyÄŤerpĂˇno" column headers
    anywhere on the page (the main billing summary table). Falls back to
    finding a table under a "DatovĂ© pĹ™enosy" heading.
    """
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if any("objem" in h for h in headers) and any("vyÄŤerpĂˇno" in h for h in headers):
            return table
    heading = _find_heading(soup, "DatovĂ© pĹ™enosy") or _find_heading(soup, "Datov")
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
