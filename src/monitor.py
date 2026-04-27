import time
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path so we can import our scripts
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from ingestion.fetch_rt import fetch_realtime_data
from ingestion.fetch_static import download_static_gtfs
from ingestion.fetch_weather import get_current_weather
from analysis.geofence import check_geofence
from db.models import get_session, WeatherRecord

def start_monitoring(interval_seconds=10, schedule_update_interval_hours=12):
    print("==================================================")
    print("STARTING CYPRUS BUS MONITORING SYSTEM")
    print("Target Route: 90")
    print("Target Stop: 11636 (Panagia Pyrgiotissa Church 1)")
    print(f"Polling Interval: {interval_seconds} seconds")
    print(f"Auto-Update Schedules: Every {schedule_update_interval_hours} hours")
    print("Press Ctrl+C to stop.")
    print("==================================================")
    
    # Run an initial schedule update on startup
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Performing initial schedule check...")
    download_static_gtfs()
    last_schedule_update = datetime.now()
    
    last_weather_update = datetime.min # Force immediate weather update
    
    try:
        while True:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"[{current_time}] Polling GTFS-RT API...")
            
            # 0a. Check if it's time to auto-update the static schedules
            if datetime.now() - last_schedule_update > timedelta(hours=schedule_update_interval_hours):
                print(f"[{current_time}] Time for routine schedule update. Checking for new GTFS data...")
                download_static_gtfs()
                last_schedule_update = datetime.now()
                
            # 0b. Fetch weather every hour to build context for ML
            if datetime.now() - last_weather_update > timedelta(hours=1):
                weather_data = get_current_weather()
                if weather_data:
                    session = get_session()
                    new_weather = WeatherRecord(
                        timestamp=weather_data['timestamp'],
                        temperature_c=weather_data['temperature_c'],
                        precipitation_mm=weather_data['precipitation_mm'],
                        wind_speed_kmh=weather_data['wind_speed_kmh'],
                        is_raining=weather_data['is_raining']
                    )
                    session.add(new_weather)
                    session.commit()
                    session.close()
                    print(f"[{current_time}] Weather logged: {weather_data['temperature_c']}°C, Raining: {weather_data['is_raining']}")
                last_weather_update = datetime.now()

            # 1. Fetch Live Data quietly
            fetch_realtime_data(quiet=True)
            
            # 2. Check the Geofence and Calculate Delays
            check_geofence(quiet=False)
            
            # 3. Wait 10 seconds (as defined by interval_seconds)
            time.sleep(interval_seconds)
            
    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")
    except Exception as e:
        print(f"\nMonitoring encountered a fatal error: {e}")
        try:
            from analysis.predict_eta import send_telegram_alert
            send_telegram_alert(f"⚠️ <b>Bus Monitor Crashed!</b>\n\nError: <code>{e}</code>\n\nNeeds a retry.")
        except Exception as alert_e:
            print(f"Failed to trigger crash alert: {alert_e}")

if __name__ == "__main__":
    start_monitoring()
