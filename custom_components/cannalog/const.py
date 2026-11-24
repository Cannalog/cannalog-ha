"""Constants for the Cannalog integration."""

DOMAIN = "cannalog"
VERSION = "0.10.0"

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
# Optional light schedule per camera (HA local time, "HH:MM:SS"). With both
# set, snapshots are only taken while the lights are on; the window may cross
# midnight (on 18:00, off 06:00).
LIGHTS_ON_FIELD = "lights_on"
LIGHTS_OFF_FIELD = "lights_off"

# Irrigation: a pump / valve entity per reservoir. Every on -> off is sent to
# Cannalog as a feed cycle (logged for every plant the reservoir feeds).
OPT_PUMPS = "pumps"  # [{reservoir_id, reservoir_name, entity_id, ml_per_minute}]
PUMP_FIELD = "pump"
PUMP_ML_FIELD = "pump_ml_per_minute"
PUMP_DOMAINS = ["switch", "valve", "input_boolean", "binary_sensor"]
PUMP_RUNNING_STATES = {"on", "open"}
MIN_RUN_SECONDS = 5  # shorter = a relay blip, not a feed
MAX_RUNS_QUEUE = 2000
RUNS_CHUNK = 100

# Live video: forward the camera's audio track too (off unless the grower
# turns it on — a mic in a grow room hears whatever is near it).
OPT_LIVE_AUDIO = "live_audio"

# Live view (on demand, only while someone has the viewer open in the app)
LIVE_FRAME_INTERVAL = 1.5  # seconds between frames
LIVE_FRAME_SIZE = (1280, 720)
LIVE_POLL_TIMEOUT = 40  # seconds; the server answers within ~25

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
    # "zone" is filled in below: a Cannalog zone is a mini environment
    "reservoir": {
        "ph": ("pH", "ph"),
        "ec": ("EC", None),
        "water_temperature": ("Water temperature", "temperature"),
        "tds": ("TDS", None),
        "orp": ("ORP", None),
        "level": ("Level", None),
    },
}

# A zone (a shelf / area inside an environment, Cannalog >= Nov 2026) takes
# the same readings as an environment.
METRICS["zone"] = METRICS["environment"]

TEMPERATURE_METRICS = {"temperature", "leaf_temperature", "dew_point", "water_temperature"}

# Offline queue cap (~4 days of 6 metrics at 1-minute interval)
MAX_QUEUE = 50_000
UPLOAD_CHUNK = 1000

SIGNAL_STATUS = f"{DOMAIN}_status_{{}}"
