import time
import os
import sys
import shutil
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
    last_env_pull = datetime.min # Retry transcriber .env delivery every 10 min

    # Cloud DB backup pacing (see 0c). The clock advances on EVERY attempt, not
    # just successful ones, and it survives a restart via a small marker file --
    # otherwise a monitor that restarts often turns "back up shortly after start"
    # into "back up every few minutes", which is half of how the B2 bucket filled.
    BACKUP_KEEP = 20                        # ~10 days of 12-hourly snapshots
    backup_marker = os.path.join(Config.BASE_DIR, "data", ".last_cloud_backup")
    backup_interval = timedelta(hours=12)
    backup_failures = 0
    last_db_backup = datetime.min
    try:
        with open(backup_marker, "r", encoding="utf-8") as f:
            last_db_backup = datetime.fromisoformat(f.read().strip())
    except Exception:
        pass
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
            # after the layer is armed). No-op while unarmed.
            #
            # THE CLOCK ADVANCES ON EVERY ATTEMPT, success or failure. The original
            # version advanced it only on success, which turned any *persistent* B2
            # failure into a retry on every 10-second loop tick: ~8,600 authorize +
            # list_buckets calls a day against a 2,500/day free Class C budget. That
            # is how the Backblaze transaction cap hit 100% on 2026-08-23 and stayed
            # there -- a capped account fails the retries too, so the loop fed itself.
            # A failure now backs off 30m -> 1h -> 2h -> 4h, capped at the normal 12h.
            if (datetime.now() - last_db_backup > backup_interval
                    and cloud_sync.is_configured()):
                last_db_backup = datetime.now()
                try:
                    with open(backup_marker, "w", encoding="utf-8") as f:
                        f.write(last_db_backup.isoformat())
                except Exception:
                    pass
                try:
                    db_path = os.path.join(Config.BASE_DIR, "data", "bus_data.db")
                    result = cloud_sync.push_db_backup(db_path)
                    if result:
                        backup_failures = 0
                        backup_interval = timedelta(hours=12)
                        print(f"[{current_time}] DB backup pushed to cloud: {result}")
                        # Client-side retention. The B2 lifecycle rule was never
                        # applied by hand, so 12-hourly ~90 MB snapshots marched the
                        # bucket toward the 10 GB free cap; a full bucket fails every
                        # upload, which is what triggered the retry storm above.
                        removed = cloud_sync.prune_old_backups(keep=BACKUP_KEEP)
                        if removed:
                            print(f"[{current_time}] Pruned {removed} old cloud backup(s); "
                                  f"kept the newest {BACKUP_KEEP}.")
                    else:
                        backup_failures += 1
                        backup_interval = min(
                            timedelta(hours=12),
                            timedelta(minutes=30) * (2 ** (backup_failures - 1)))
                        print(f"[{current_time}] DB backup FAILED ({backup_failures}x in a row). "
                              f"Next attempt in {backup_interval}.")
                        if backup_failures == 3:
                            try:
                                from analysis.predict_eta import send_telegram_alert
                                open_, reason, until = cloud_sync.breaker_status()
                                send_telegram_alert(
                                    f"☁️ Cloud DB backup has failed 3 times in a row"
                                    + (f" ({reason}, paused until {until} UTC)" if open_ else "")
                                    + ". Backups are NOT reaching B2 - check the bucket's "
                                      "storage/transaction caps. Local data collection is "
                                      "unaffected; retries are backing off, not hammering.")
                            except Exception as alert_err:
                                print(f"Backup alert failed: {alert_err}")
                except Exception as e:
                    print(f"Cloud DB backup skipped: {e}")

            # 0d. Sync project .env files FROM cloud storage (Backblaze B2 is the
            # canonical source for these secrets). This is the out-of-band recovery
            # channel for DELIVERING or ROTATING a bot's secrets when its own inbound
            # .env handler is offline - e.g. after a leaked bus-bot token is revoked,
            # which is exactly what takes the normal DM channel down. The Admin Hub
            # ships code; B2 ships secrets; this still-running monitor is the puller.
            #
            # Runs every 10 min. For each mapping it pulls the object to a temp file,
            # validates it, and replaces the local .env ONLY when the content differs
            # (idempotent: no needless writes, no overwrite loop, no revert once in
            # sync). The previous .env is kept as .env.bak. A change takes effect when
            # that bot is restarted via the Admin Hub. To rotate: upload the new .env
            # to B2 (web console) as the object below; the server picks it up here.
            if datetime.now() - last_env_pull > timedelta(minutes=10):
                last_env_pull = datetime.now()
                projects_root = os.path.dirname(Config.BASE_DIR)
                env_sources = [
                    # (B2 object name,   local .env path,                                              required marker)
                    ("bus/.env",         os.path.join(Config.BASE_DIR, ".env"),                        "TELEGRAM_BOT_TOKEN"),
                    ("transcriber/.env", os.path.join(projects_root, "Constan_transcriber_telegram_bot", ".env"), "PUBLIC_BOT_TOKEN"),
                ]
                for obj, dest, marker in env_sources:
                    try:
                        if not (cloud_sync.is_configured() and os.path.isdir(os.path.dirname(dest))):
                            continue
                        tmp = dest + ".fromcloud"
                        if os.path.exists(tmp):
                            os.remove(tmp)
                        # Pull to temp (no_clobber safe: tmp was just removed), validate it
                        # really is that .env, then swap in only if it actually changed.
                        if not cloud_sync.pull(obj, tmp, validate_contains=marker, no_clobber=True):
                            continue
                        with open(tmp, "rb") as f:
                            new_bytes = f.read()
                        cur_bytes = b""
                        if os.path.exists(dest):
                            with open(dest, "rb") as f:
                                cur_bytes = f.read()
                        if new_bytes and new_bytes != cur_bytes:
                            if os.path.exists(dest):
                                shutil.copy2(dest, dest + ".bak")
                            os.replace(tmp, dest)
                            print(f"[{current_time}] {obj} synced from cloud -> {dest}. Restart that bot via the Admin Hub to apply.")
                            # NEVER replace a .env silently. The one time this ran
                            # quietly it reverted a freshly DM-delivered allowlist to
                            # the stale cloud copy, and nobody knew until a power
                            # outage restarted that bot on the old file weeks later.
                            try:
                                from analysis.predict_eta import send_telegram_alert
                                send_telegram_alert(
                                    f"☁️ {obj}: the local .env was just REPLACED from the "
                                    f"cloud copy (old file kept as .env.bak). Expected if you "
                                    f"uploaded it to B2 yourself; if NOT, a stale cloud copy "
                                    f"has overwritten a newer local file - resend the right "
                                    f".env by DM and compare fingerprints with /env_check in "
                                    f"the Hub. Applies at that bot's next restart.")
                            except Exception as alert_err:
                                print(f"Env-sync alert failed: {alert_err}")
                        else:
                            os.remove(tmp)
                    except Exception as e:
                        print(f"Cloud .env sync skipped for {obj}: {e}")

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
