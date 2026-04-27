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

    @classmethod
    def ensure_directories(cls):
        os.makedirs(cls.STATIC_DATA_DIR, exist_ok=True)
        os.makedirs(cls.RT_DATA_DIR, exist_ok=True)
