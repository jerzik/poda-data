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
# for requests that look like bots).
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 HomeAssistant-PODA-data"
    ),
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
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
