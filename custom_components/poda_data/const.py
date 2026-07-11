"""Constants for the PODA data integration."""
from datetime import timedelta

DOMAIN = "poda_data"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"

DEFAULT_SCAN_INTERVAL_HOURS = 6

BASE_URL = "https://klient.poda.cz"
LOGIN_URL = f"{BASE_URL}/site/login"
BILLING_URL = f"{BASE_URL}/mobily/vyuctovani"

# Headers to look like a normal browser (some Yii2 apps behave differently
# for requests that look like bots, and this portal sits behind Cloudflare
# with fairly aggressive fingerprinting scripts loaded, so we mirror a real
# Firefox request as closely as a plain HTTP client reasonably can).
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
        "Gecko/20100101 Firefox/152.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "DNT": "1",
    "Sec-GPC": "1",
}

ATTR_NUMBER = "cislo"
ATTR_NAME = "nazev"

SENSOR_CALLS_MINUTES = "calls_minutes"
SENSOR_CALLS_PRICE = "calls_price"
SENSOR_SMS_COUNT = "sms_count"
SENSOR_SMS_PRICE = "sms_price"
SENSOR_DATA_USED = "data_used"
SENSOR_DATA_LIMIT = "data_limit"

PLATFORMS = ["sensor"]

MIN_SCAN_INTERVAL = timedelta(hours=1)
