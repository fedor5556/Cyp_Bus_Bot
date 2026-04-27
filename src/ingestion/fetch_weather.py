import openmeteo_requests
import requests_cache
from retry_requests import retry
import os
import sys
from datetime import datetime

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

# Limassol coordinates for weather data
LIMASSOL_LAT = 34.675
LIMASSOL_LON = 33.0333

def get_current_weather():
    """
    Fetches the current weather for Limassol using the Open-Meteo API.
    Returns a dictionary with temperature, wind speed, and precipitation.
    """
    # Setup the Open-Meteo API client with cache and retry on error
    cache_session = requests_cache.CachedSession('.cache', expire_after=3600) # Cache for 1 hour
    retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
    openmeteo = openmeteo_requests.Client(session=retry_session)

    # Make sure all required weather variables are listed here
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LIMASSOL_LAT,
        "longitude": LIMASSOL_LON,
        "current": ["temperature_2m", "precipitation", "wind_speed_10m"]
    }
    
    try:
        responses = openmeteo.weather_api(url, params=params)
        
        # Process first location. Add a for-loop for multiple locations or weather models
        response = responses[0]
        
        current = response.Current()
        current_temperature_2m = current.Variables(0).Value()
        current_precipitation = current.Variables(1).Value()
        current_wind_speed_10m = current.Variables(2).Value()
        
        is_raining = current_precipitation > 0.1
        
        return {
            "temperature_c": round(current_temperature_2m, 1),
            "precipitation_mm": round(current_precipitation, 1),
            "wind_speed_kmh": round(current_wind_speed_10m, 1),
            "is_raining": is_raining,
            "timestamp": datetime.now()
        }
    except Exception as e:
        print(f"Error fetching weather data: {e}")
        return None

if __name__ == "__main__":
    weather = get_current_weather()
    if weather:
        print(f"Limassol Weather:")
        print(f"Temperature: {weather['temperature_c']} °C")
        print(f"Precipitation: {weather['precipitation_mm']} mm (Raining: {weather['is_raining']})")
        print(f"Wind Speed: {weather['wind_speed_kmh']} km/h")