import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # GTFS URLs
    STATIC_GTFS_URL_TEMPLATE = os.getenv("STATIC_GTFS_URL_TEMPLATE", "https://www.motionbuscard.org.cy/opendata/downloadfile?file=GTFS%5C{siri_code}_google_transit.zip&rel=True")
    GTFS_RT_URL = os.getenv("GTFS_RT_URL", "http://20.19.98.194:8328/Api/api/gtfs-realtime")

    # City SIRI Codes based on Cyprus Bus Network
    CITIES = {
        "Nicosia": 9,
        "Limassol": 6,
        "Larnaca": 10,
        "Pafos": 2,
        "Famagusta": 4,
        "Intercity": 5,
        "Pame_Express": 11
    }

    # Paths
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    RAW_DATA_DIR = os.path.join(BASE_DIR, "data", "raw")
    STATIC_DATA_DIR = os.path.join(RAW_DATA_DIR, "static")
    RT_DATA_DIR = os.path.join(RAW_DATA_DIR, "rt")

    # Database
    DATABASE_URL = os.getenv("DATABASE_URL")

    # Admin authorization: numeric Telegram user IDs (unforgeable, NOT secrets).
    # Parsed from the bus bot's own ADMIN_TELEGRAM_ID, which already ships on the
    # server, so admin-gated handlers work with zero bootstrap. Gate on these IDs,
    # never on the spoofable username. Empty set => fail closed (no admin).
    ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_TELEGRAM_ID", "").split(",") if x.strip().isdigit()}

    # Backblaze B2 transfer layer (see src/cloud_sync.py).
    # The bucket NAME is not a secret (the bucket itself is private), so it ships
    # as a committed default to avoid editing env on the server; the B2_BUCKET env
    # var still overrides. Must match the real bucket created in the B2 console.
    # The application KEY is the only secret; it arrives out-of-band via Telegram
    # (DM a .json or /armb2) and is stored gitignored at B2_KEY_PATH (never git).
    B2_BUCKET = os.getenv("B2_BUCKET", "cyprus-bus-bot")
    B2_KEY_PATH = os.getenv("B2_KEY_PATH", os.path.join(BASE_DIR, "secrets", "b2_key.json"))

    @classmethod
    def ensure_directories(cls):
        os.makedirs(cls.STATIC_DATA_DIR, exist_ok=True)
        os.makedirs(cls.RT_DATA_DIR, exist_ok=True)
