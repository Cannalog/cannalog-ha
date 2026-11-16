"""Constants for the Cannalog integration."""

DOMAIN = "cannalog"
VERSION = "0.4.0"

DEFAULT_API_URL = "https://api.cannalog.co.za"

CONF_API_URL = "api_url"
CONF_TOKEN = "token"
CONF_CONNECTION_ID = "connection_id"
CONF_USER_ID = "user_id"
CONF_USER_NAME = "user_name"
CONF_PAIRING_CODE = "pairing_code"

OPT_MAPPINGS = "mappings"
OPT_INTERVAL = "interval"  # minutes
DEFAULT_INTERVAL = 5
INTERVAL_CHOICES = [1, 2, 5, 10, 15, 30]

# Camera snapshots: latest frame per environment (Cannalog keeps only the newest).
OPT_CAMERAS = "cameras"  # [{environment_id, environment_name, entity_id}]
OPT_SNAPSHOT_INTERVAL = "snapshot_interval"  # minutes
DEFAULT_SNAPSHOT_INTERVAL = 30
SNAPSHOT_INTERVAL_CHOICES = [5, 10, 15, 30, 60]
CAMERA_FIELD = "camera"

# Readings per Cannalog target type. Must match the API's METRICS table.
# value: (label, device_class used to pre-filter the entity picker or None)
METRICS = {
    "environment": {
        "temperature": ("Air temperature", "temperature"),
        "humidity": ("Relative humidity", "humidity"),
        "vpd": ("VPD", None),
        "leaf_temperature": ("Leaf temperature", "temperature"),
        "dew_point": ("Dew point", "temperature"),
        "co2": ("CO2", "carbon_dioxide"),
        "ppfd": ("PPFD", None),
    },
    "reservoir": {
        "ph": ("pH", "ph"),
        "ec": ("EC", None),
        "water_temperature": ("Water temperature", "temperature"),
        "tds": ("TDS", None),
        "orp": ("ORP", None),
        "level": ("Level", None),
    },
}

TEMPERATURE_METRICS = {"temperature", "leaf_temperature", "dew_point", "water_temperature"}

# Offline queue cap (~4 days of 6 metrics at 1-minute interval)
MAX_QUEUE = 50_000
UPLOAD_CHUNK = 1000

SIGNAL_STATUS = f"{DOMAIN}_status_{{}}"
