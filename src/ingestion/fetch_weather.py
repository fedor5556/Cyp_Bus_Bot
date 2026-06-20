import requests
import os
import sys
import time
from datetime import datetime

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

# Limassol coordinates for weather data
LIMASSOL_LAT = 34.675
LIMASSOL_LON = 33.0333

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def get_current_weather():
    """
    Fetches the current weather for Limassol using the Open-Meteo JSON API.
    Returns a dict with temperature, wind speed, and precipitation, or None on failure.

    Uses a plain `requests` GET instead of the previous openmeteo_requests +
    requests_cache + retry_requests stack. That chain pulled in an attrs model whose
    forward-reference annotation (`RequestsCookieJar`) fails to resolve under Python
    3.14's typing.get_type_hints, so EVERY call raised `NameError` and was swallowed
    here (returning None) -- weather silently stopped logging while the table sat
    frozen. Open-Meteo's defaults already return the exact units we store
    (temperature_2m -> degC, precipitation -> mm, wind_speed_10m -> km/h), and the
    monitor already gates this to once per hour, so the old 1-hour requests_cache was
    redundant. A short manual retry replaces retry_requests for transient blips.
    """
    params = {
        "latitude": LIMASSOL_LAT,
        "longitude": LIMASSOL_LON,
        "current": "temperature_2m,precipitation,wind_speed_10m",
    }

    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)
            resp.raise_for_status()
            current = resp.json()["current"]

            precipitation = float(current["precipitation"])
            return {
                "temperature_c": round(float(current["temperature_2m"]), 1),
                "precipitation_mm": round(precipitation, 1),
                "wind_speed_kmh": round(float(current["wind_speed_10m"]), 1),
                "is_raining": precipitation > 0.1,
                "timestamp": datetime.now(),
            }
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2)

    print(f"Error fetching weather data: {last_err}")
    return None


if __name__ == "__main__":
    weather = get_current_weather()
    if weather:
        print(f"Limassol Weather:")
        print(f"Temperature: {weather['temperature_c']} °C")
        print(f"Precipitation: {weather['precipitation_mm']} mm (Raining: {weather['is_raining']})")
        print(f"Wind Speed: {weather['wind_speed_kmh']} km/h")
