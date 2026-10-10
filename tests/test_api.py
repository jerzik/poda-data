"""Portal regression tests with fictional data only."""
import importlib
import logging
from pathlib import Path
import sys
import types
import unittest

package = types.ModuleType("poda_test")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/poda_data")]
sys.modules["poda_test"] = package
api = importlib.import_module("poda_test.api")
from bs4 import BeautifulSoup

SUMMARY = """<table><tr><th>Číslo</th><th>Název</th><th>Objem</th><th>Vyčerpáno</th></tr>
<tr><td>000000001</td><td>Test tarif</td><td>2 GB</td><td>512 MB</td></tr>
<tr><td>000000002</td><td>Bez provozu</td><td>1 GB</td><td>0 MB</td></tr></table>"""
LOGIN = """<form id="login-form" action="/login">
<input name="_csrf" value="private-token">
<input name="LoginForm[username]"><input name="LoginForm[password]" type="password">
</form>"""

class Response:
    def __init__(self, text, status=200):
        self.body, self.status = text, status
        self.headers = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def raise_for_status(self):
        if self.status >= 400: raise api.aiohttp.ClientError("secret-url")
    async def text(self): return self.body
    async def read(self): return self.body.encode()

class Session:
    def __init__(self, responses, post_body=SUMMARY, post_status=200):
        self.responses, self.post_body, self.post_status = iter(responses), post_body, post_status
        self.sent = None
    def get(self, *args, **kwargs): return Response(next(self.responses))
    def post(self, url, **kwargs):
        self.sent = kwargs["data"]
        return Response(self.post_body, self.post_status)

class ParserTests(unittest.TestCase):
    def test_normalization(self):
        for value in ("000000001", "+420 000 000 001", "00420000000001"):
            self.assertEqual(api._normalize_number(value), "000000001")
        for value in ("", "123", "420", "123456789012", "Součet 2"):
            self.assertEqual(api._normalize_number(value), "")

    def test_unused_numbers(self):
        stats = {}
        soup = BeautifulSoup(SUMMARY, "html.parser")
        api.PodaClient._discover_numbers(soup, stats)
        api.PodaClient._parse_data_usage(soup, stats)
        self.assertEqual(set(stats), {"000000001", "000000002"})
        self.assertEqual(stats["000000001"].data_limit_mb, 2048)
        self.assertEqual(stats["000000002"].data_used_mb, 0)

    def test_excludes_destination_column(self):
        soup = BeautifulSoup("<table><tr><th>Cílové číslo</th></tr><tr><td>000000002</td></tr></table>", "html.parser")
        stats = {}
        api.PodaClient._discover_numbers(soup, stats)
        self.assertEqual(stats, {})

    def test_call_aggregation(self):
        stats = {}
        api.PodaClient._parse_calls_csv(b"src,dst,billsec,price\n00420000000001,000000002,60,1.5\n000000001,000000002,30,2\n", stats)
        self.assertEqual(set(stats), {"000000001"})
        self.assertEqual(stats["000000001"].calls_minutes, 1.5)
        self.assertEqual(stats["000000001"].calls_price, 3.5)

    def test_sms(self):
        stats = {}
        api.PodaClient._parse_sms_csv(b"src,dst,price\n000000002,000000001,1\n", stats)
        self.assertEqual(stats["000000002"].sms_count, 1)

    def test_data_sessions(self):
        stats = {}
        api.PodaClient._parse_data_csv(b"src,start,kb,price,zone\n000000001,,1024,0,1\n", stats)
        self.assertEqual(stats["000000001"].data_used_mb, 1)

    def test_untrusted_urls(self):
        for target in ("https://evil.invalid/", "//evil.invalid/", "http://klient.poda.cz/", "https://user@klient.poda.cz/"):
            with self.assertRaises(api.PodaConnectionError):
                api._portal_url(api.BASE_URL, target)
        self.assertTrue(api._portal_url(api.BASE_URL, "/export").startswith(api.BASE_URL))

class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_and_private_logging(self):
        session = Session([LOGIN, SUMMARY])
        client = api.PodaClient(session, "private-user", "private-password")
        with self.assertLogs(api._LOGGER, logging.DEBUG) as captured:
            stats = await client.async_get_stats()
        self.assertEqual(set(stats), {"000000001", "000000002"})
        self.assertEqual(session.sent["LoginForm[username]"], "private-user")
        for secret in ("private-user", "private-password", "private-token", "000000001", "Test tarif"):
            self.assertNotIn(secret, "\n".join(captured.output))

    async def test_rejected_login(self):
        client = api.PodaClient(Session([LOGIN], LOGIN), "user", "password")
        with self.assertRaisesRegex(api.PodaAuthError, "^Login rejected by portal$"):
            await client.async_login()

    async def test_http_failure(self):
        client = api.PodaClient(Session([LOGIN], SUMMARY, 500), "user", "password")
        with self.assertRaises(api.PodaConnectionError):
            await client.async_login()
        self.assertFalse(client._logged_in)

    async def test_expired_session(self):
        client = api.PodaClient(Session([LOGIN, LOGIN, SUMMARY]), "user", "password")
        client._logged_in = True
        self.assertEqual(len(await client.async_get_stats()), 2)

    async def test_persistent_login_page(self):
        client = api.PodaClient(Session([LOGIN, LOGIN, LOGIN]), "user", "password")
        client._logged_in = True
        with self.assertRaises(api.PodaAuthError):
            await client.async_get_stats()
