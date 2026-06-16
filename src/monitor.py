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
from analysis import eta
from db.models import get_session, migrate_db, WeatherRecord
from config import Config
import cloud_sync

def start_monitoring(interval_seconds=10, schedule_update_interval_hours=12):
    print("==================================================")
    print("STARTING CYPRUS BUS MONITORING SYSTEM")
    print("Target Route: 90")
    print("Target Stop: 11636 (Panagia Pyrgiotissa Church 1)")
    print(f"Polling Interval: {interval_seconds} seconds")
    print(f"Auto-Update Schedules: Every {schedule_update_interval_hours} hours")
    print("Press Ctrl+C to stop.")
    print("==================================================")
    
    # Apply any pending schema migrations BEFORE the loop (adds multi-stop
    # capture columns + the (trip_id, stop_id) unique index). Idempotent; run
    # under PRAGMA busy_timeout so it waits out the ETA bot's concurrent writes.
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running database migrations...")
    try:
        migrate_db()
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] migrate_db failed: {e}")

    # Run an initial schedule update on startup
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Performing initial schedule check...")
    download_static_gtfs()
    last_schedule_update = datetime.now()

    last_weather_update = datetime.min # Force immediate weather update
    last_db_backup = datetime.min # Push a DB backup early, then every 12h
    last_env_pull = datetime.min # Retry transcriber .env delivery every 10 min
    last_prediction_log = datetime.min # Snapshot ETA predictions every 60s (drift dataset)
    
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

            # 0c. Push a DB backup to cloud storage every 12h (and once shortly
            # after the layer is armed). No-op while unarmed; last_db_backup only
            # advances on a successful push, so arming triggers a prompt backup.
            if datetime.now() - last_db_backup > timedelta(hours=12):
                try:
                    if cloud_sync.is_configured():
                        db_path = os.path.join(Config.BASE_DIR, "data", "bus_data.db")
                        result = cloud_sync.push_db_backup(db_path)
                        if result:
                            print(f"[{current_time}] DB backup pushed to cloud: {result}")
                            last_db_backup = datetime.now()
                except Exception as e:
                    print(f"Cloud DB backup skipped: {e}")

            # 0d. Deliver the Voice Transcriber's .env from cloud storage if it is
            # not on this machine yet. Retries every 10 min (so it works no matter
            # when the layer is armed or the object is uploaded) and stops touching
            # the network once the file exists - its existence is the sentinel.
            # The primary delivery path is now a Telegram DM to the bot; this is
            # the fallback for when that is forgotten.
            if datetime.now() - last_env_pull > timedelta(minutes=10):
                last_env_pull = datetime.now()
                try:
                    sibling = os.path.join(os.path.dirname(Config.BASE_DIR), "Constan_transcriber_telegram_bot")
                    transcriber_env = os.path.join(sibling, ".env")
                    if (cloud_sync.is_configured() and os.path.isdir(sibling)
                            and not os.path.exists(transcriber_env)):
                        if cloud_sync.pull("transcriber/.env", transcriber_env,
                                           validate_contains="PUBLIC_BOT_TOKEN", no_clobber=True):
                            print(f"[{current_time}] Transcriber .env delivered from cloud storage.")
                except Exception as e:
                    print(f"Cloud transcriber .env pull skipped: {e}")

            # 1. Fetch Live Data quietly
            fetch_realtime_data(quiet=True)

            # 2. Check stop crossings and Calculate Delays. Isolated so one bad
            # stop / projection in the heavier multi-stop pass can't crash the
            # whole monitor loop.
            try:
                check_geofence(quiet=False)
            except Exception as e:
                print(f"[{current_time}] check_geofence error (continuing): {e}")

            # 2b. Snapshot the bot's own forward ETAs every 60s (PredictionLog /
            # drift dataset). Reuses the pings just fetched - no extra GTFS-RT call.
            # Same robustness rule as geofence: a bad prediction can't crash the loop.
            if datetime.now() - last_prediction_log > timedelta(seconds=60):
                last_prediction_log = datetime.now()
                try:
                    pred_session = get_session()
                    try:
                        n = eta.log_predictions(pred_session)
                    finally:
                        pred_session.close()
                    if n:
                        print(f"[{current_time}] Logged {n} ETA prediction(s).")
                except Exception as e:
                    print(f"[{current_time}] log_predictions error (continuing): {e}")

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
    import log_tee
    log_tee.setup("monitor")
    start_monitoring()
