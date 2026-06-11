import os
import re
import sys
import json
import shutil
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# Add parent directory to path so we can import config and models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.models import VehiclePosition, get_session
from analysis.geofence import haversine, STOP_LAT, STOP_LON, RADIUS_METERS
from config import Config

# Target stops based on direction
TARGET_STOPS = {
    "10900012": "7604", # Towards Sanida (Panagia Pyrgiotissa Church 1)
    "10900011": "5411"  # Towards Lemesos (Panagia Pyrgiotissa Church 2)
}

def calculate_sma_speed(session, vehicle_id, current_pos, num_pings=10):
    pings = session.query(VehiclePosition).filter(
        VehiclePosition.vehicle_id == vehicle_id,
        VehiclePosition.timestamp <= current_pos.timestamp
    ).order_by(VehiclePosition.timestamp.desc()).limit(num_pings).all()
    
    if len(pings) < 2: return 0.0
    
    speeds = []
    for i in range(len(pings) - 1):
        p1, p2 = pings[i], pings[i+1]
        time_diff = (p1.timestamp - p2.timestamp).total_seconds()
        if time_diff > 0:
            dist = haversine(p1.latitude, p1.longitude, p2.latitude, p2.longitude)
            if dist < 1500: # Filter GPS noise
                speeds.append(dist / time_diff)
    
    return sum(speeds) / len(speeds) if speeds else 0.0

def get_trip_state(trip_id, pos):
    """
    Finds the closest stop to the ping, calculates delay, and returns the stop sequence.
    Returns: (delay_sec, current_sequence, target_sequence, target_scheduled_dt)
    """
    stop_times_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stop_times.txt')
    stops_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stops.txt')
    
    if not os.path.exists(stop_times_path) or not os.path.exists(stops_path): 
        return 0, -1, -1, None
    
    # Load trip schedule
    st_df = pd.read_csv(stop_times_path, usecols=['trip_id', 'arrival_time', 'stop_id', 'stop_sequence'], dtype=str)
    trip_df = st_df[st_df['trip_id'] == trip_id].copy()
    if trip_df.empty: return 0, -1, -1, None
    trip_df['stop_sequence'] = trip_df['stop_sequence'].astype(int)
    
    # Identify target stop sequence and schedule for this specific trip
    target_stop_id = TARGET_STOPS.get(pos.route_id)
    if not target_stop_id: return 0, -1, -1, None
    
    target_row = trip_df[trip_df['stop_id'] == target_stop_id]
    if target_row.empty: return 0, -1, -1, None
    
    target_seq = target_row.iloc[0]['stop_sequence']
    target_time_str = target_row.iloc[0]['arrival_time']
    
    # Base date logic for time parsing
    base_date = pos.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    
    def parse_time(t_str):
        h, m, s = map(int, t_str.split(':'))
        return base_date + timedelta(hours=h, minutes=m, seconds=s)
        
    target_scheduled_dt = parse_time(target_time_str)

    # Load stops to find closest
    stops_df = pd.read_csv(stops_path, usecols=['stop_id', 'stop_lat', 'stop_lon'], dtype=str)
    merged = pd.merge(trip_df, stops_df, on='stop_id')
    merged['stop_lat'] = merged['stop_lat'].astype(float)
    merged['stop_lon'] = merged['stop_lon'].astype(float)
    
    def calc_dist(row):
        return haversine(pos.latitude, pos.longitude, row['stop_lat'], row['stop_lon'])
        
    merged['dist'] = merged.apply(calc_dist, axis=1)
    closest = merged.loc[merged['dist'].idxmin()]
    
    current_seq = closest['stop_sequence']
    sched_dt = parse_time(closest['arrival_time'])
    delay_sec = (pos.timestamp - sched_dt).total_seconds()
    
    return delay_sec, current_seq, target_seq, target_scheduled_dt

def get_prediction_text():
    # Perform a reactive check to ensure we have the absolute latest data before querying the DB
    try:
        from ingestion.fetch_rt import fetch_realtime_data
        fetch_realtime_data(quiet=True)
    except Exception as e:
        print(f"Reactive fetch failed: {e}")

    output = []
    output.append("==================================================")
    output.append("LIVE ETA PREDICTION: PYRGOS CHURCH")
    output.append("==================================================")
    output.append("")
    
    session = get_session()
    
    # Robust threshold: 15 minutes before the latest ping in DB OR current time
    # This prevents timezone mismatches from hiding data
    max_db_pos = session.query(VehiclePosition).order_by(VehiclePosition.timestamp.desc()).first()
    max_db_time = max_db_pos.timestamp if max_db_pos else datetime.now()
    
    # We take the later of the two, then go back 15 mins
    effective_now = max(datetime.now(), max_db_time)
    threshold = effective_now - timedelta(minutes=15)
    
    latest_positions = session.query(VehiclePosition).filter(
        VehiclePosition.route_id.in_(["10900011", "10900012"]),
        VehiclePosition.timestamp >= threshold
    ).order_by(VehiclePosition.timestamp.desc()).all()
    
    if not latest_positions:
        output.append("No active vehicles on Route 90 found.")
        output.append("-" * 50)
        
        from analysis.schedule import get_next_scheduled_arrival
        next_lemesos = get_next_scheduled_arrival("10900011", TARGET_STOPS["10900011"])
        next_sanida = get_next_scheduled_arrival("10900012", TARGET_STOPS["10900012"])
        
        if next_lemesos:
            output.append(f"Next Scheduled (Towards Lemesos): {next_lemesos.strftime('%Y-%m-%d %H:%M')}")
        if next_sanida:
            output.append(f"Next Scheduled (Towards Sanida):  {next_sanida.strftime('%Y-%m-%d %H:%M')}")
            
        session.close()
        return "\n".join(output)

    seen_vehicles = {}
    for pos in latest_positions:
        if pos.vehicle_id not in seen_vehicles:
            seen_vehicles[pos.vehicle_id] = pos

    for vehicle_id, pos in seen_vehicles.items():
        delay_sec, current_seq, target_seq, target_sched = get_trip_state(pos.trip_id, pos)

        time_since_ping = (datetime.now() - pos.timestamp).total_seconds()
        is_stale_ping = time_since_ping > 120
        is_stationary = getattr(pos, 'is_stationary', False)

        # Schedule safeguard: filter out buses that have no schedule data and are stale/stationary
        if not target_sched and (is_stale_ping or is_stationary):
            continue

        raw_sma_speed_mps = calculate_sma_speed(session, vehicle_id, pos, num_pings=10)

        # Halt Extrapolation for Stale/Stationary Pings
        if is_stale_ping or is_stationary:
            sma_speed_mps = 0.0
            extrapolate_time = 0.0
        else:
            sma_speed_mps = raw_sma_speed_mps
            extrapolate_time = max(0, time_since_ping)

        sma_speed_kmh = sma_speed_mps * 3.6

        estimated_movement = sma_speed_mps * extrapolate_time
        raw_distance = haversine(STOP_LAT, STOP_LON, pos.latitude, pos.longitude)
        smooth_distance = max(0, raw_distance - estimated_movement)

        direction = "Towards Lemesos" if pos.route_id == "10900011" else "Towards Sanida"

        status_tag = ""
        if is_stale_ping:
            status_tag = " [STALE DATA]"
        elif is_stationary:
            status_tag = " [STATIONARY]"

        output.append(f"Vehicle {pos.vehicle_id} ({direction}){status_tag}")

        if smooth_distance <= RADIUS_METERS:
            output.append("  Status: AT PYRGOS CHURCH")
            output.append("-" * 50)
            continue

        # 1. ALREADY PASSED CHECK
        if target_seq != -1 and current_seq > target_seq:
            output.append(f"  Distance to Pyrgos Church: {smooth_distance/1000:.1f} km (Speed: {sma_speed_kmh:.1f} km/h)")
            output.append(f"  Status: Passed Pyrgos on current trip (At or heading to terminal)")
            
            # Provide movement-based ETA for the return trip
            if sma_speed_mps > 1.0:
                eta = (smooth_distance / sma_speed_mps) / 60
                output.append(f"  --> Next ETA to Pyrgos: ~{eta:.1f} minutes (Movement-based)")
            else:
                # Terminal wait fallback: assume 30 km/h (8.33 m/s) return speed
                eta = (smooth_distance / 8.33) / 60
                output.append(f"  --> Next ETA to Pyrgos: ~{eta:.1f} minutes (Assuming 30km/h once moving)")
            output.append("-" * 50)
            continue
            
        output.append(f"  Distance to Pyrgos Church: {smooth_distance/1000:.1f} km (Speed: {sma_speed_kmh:.1f} km/h)")
        
        if target_sched:
            delay_min = delay_sec / 60
            
            if delay_min > 1:
                status_str = f"Running {int(abs(delay_min))} mins LATE"
            elif delay_min < -15:
                status_str = f"Parked / Waiting for scheduled departure ({int(abs(delay_min))} mins until active)"
            elif delay_min < -1:
                status_str = f"Running {int(abs(delay_min))} mins EARLY"
            else:
                status_str = "Running ON TIME"
                
            output.append(f"  Timetable Schedule: {target_sched.strftime('%H:%M:%S')}")
            output.append(f"  Current Status: {status_str}")
            
            # ETA Calculation
            projected_arrival = target_sched + timedelta(seconds=delay_sec)
            profile_eta_min = (projected_arrival - datetime.now()).total_seconds() / 60
            
            if sma_speed_mps > 1.0:
                move_eta_min = (smooth_distance / sma_speed_mps) / 60
                weight_move = max(0, min(1, 1 - (smooth_distance / 12000))) 
                hybrid_eta = (move_eta_min * weight_move * 0.8) + (profile_eta_min * (1 - (weight_move * 0.8)))
                
                expected_time = datetime.now() + timedelta(minutes=hybrid_eta)
                output.append(f"  --> EXPECTED ARRIVAL: {expected_time.strftime('%H:%M:%S')} (in ~{hybrid_eta:.1f} minutes)")
            else:
                expected_time = datetime.now() + timedelta(minutes=max(0, profile_eta_min))
                output.append(f"  --> EXPECTED ARRIVAL: {expected_time.strftime('%H:%M:%S')} (in ~{max(0, profile_eta_min):.1f} minutes) [Stationary]")
        else:
            if sma_speed_mps > 1.0:
                eta = (smooth_distance / sma_speed_mps) / 60
                output.append(f"  --> Predicted ETA: ~{eta:.1f} minutes (No schedule data)")
            else:
                output.append("  --> Status: Unknown (No schedule, not moving)")

        output.append("-" * 50)
        
    session.close()
    return "\n".join(output)

def send_telegram_alert(message):
    """Sends a message via Telegram API for alerts/crashes."""
    import requests
    from dotenv import load_dotenv
    load_dotenv()
    
    # You can set CRASH_BOT_TOKEN specifically, or it will fall back to the main token
    token = os.environ.get("CRASH_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            print(f"Failed to send Telegram alert: {e}")
    else:
        print("Note: To receive Telegram alerts, ensure TELEGRAM_CHAT_ID and a BOT_TOKEN are set in .env")

def predict_live_eta():
    print(get_prediction_text())

async def bot_handler(update, context):
    user = update.message.from_user
    allowed_users_env = os.environ.get("ALLOWED_TELEGRAM_USERS")
    
    if allowed_users_env:
        allowed_users = [u.strip().lstrip("@") for u in allowed_users_env.split(",")]
        if user.username not in allowed_users:
            print(f"Unauthorized access attempt from user: {user.username}")
            return

    text = get_prediction_text()
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")

# --- Admin-gated inbound file channel + cloud transfer commands -------------------
# This ETA bot is PUBLIC by design, so these handlers gate on the numeric Telegram
# user ID (Config.ADMIN_IDS, unforgeable). They restore the inbound-file capability
# the send-only Admin Hub lacks WITHOUT touching the Hub. Two inbound paths:
#   1. DM a .env document (caption or filename names the target project) -> written
#      to that sibling project's folder. Telegram's ~20 MB inbound cap is plenty.
#   2. DM a .json document (or /armb2) carrying the B2 application key -> arms the
#      cloud layer in cloud_sync.py for large files (DB backups, ML models).

# Holds the single-instance lock socket for the process lifetime (see start_bot).
_SINGLE_INSTANCE_LOCK = None

# Friendly names for .env routing. Any exact sibling folder name also works, so
# new projects need no code change; aliases are just convenience. "bus" resolves
# at runtime because the project folder is named differently on dev vs server.
ENV_TARGET_ALIASES = {
    "transcriber": "Constan_transcriber_telegram_bot",
}


def _is_admin(update):
    """Numeric-ID admin gate. Fails closed when Config.ADMIN_IDS is empty (no env)."""
    user = update.effective_user
    return bool(user) and user.id in Config.ADMIN_IDS


def _store_b2_key(key_id, app_key):
    """Atomically persist the B2 application key where cloud_sync expects it."""
    key_path = Config.B2_KEY_PATH
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    part_path = key_path + ".part"
    with open(part_path, "w", encoding="utf-8") as f:
        json.dump({"keyID": key_id, "applicationKey": app_key}, f)
    os.replace(part_path, key_path)


async def document_upload_handler(update, context):
    """Route admin document uploads: .env -> project env delivery, .json -> B2 key.
    Non-admins get silence (public bot)."""
    if not _is_admin(update):
        return
    doc = update.message.document if update.message else None
    if doc is None:
        return

    name = (doc.file_name or "").lower()
    if name.endswith(".env"):
        await _env_upload(update, context, doc)
    elif name.endswith(".json"):
        await _b2_key_upload(update, context, doc)
    else:
        await update.message.reply_text(
            "Unsupported file. Send a project .env (caption = project folder or alias) "
            "or a B2 application key as .json."
        )


async def _env_upload(update, context, doc):
    """Write a DM'd .env into a sibling project's folder. The target comes from the
    message caption, or from the filename stem (e.g. transcriber.env). The previous
    .env (if any) is kept as .env.bak. The file only takes effect when that project
    is restarted via the Admin Hub."""
    msg = update.message
    caption = (msg.caption or "").strip()
    fname = doc.file_name or ""
    stem = fname[:-4].strip() if fname.lower().endswith(".env") else ""
    target = caption or stem
    if not target:
        await msg.reply_text(
            "Which project is this .env for? Add the project folder name (or an alias "
            "like 'transcriber') as the file caption, or name the file e.g. transcriber.env."
        )
        return

    folder_name = ENV_TARGET_ALIASES.get(target.lower(), target)
    if target.lower() == "bus":
        folder_name = os.path.basename(Config.BASE_DIR)
    # Strict charset = no path traversal (no slashes, dots, drive letters).
    if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_name):
        await msg.reply_text(f"Rejected: '{folder_name}' is not a valid project folder name.")
        return

    projects_root = os.path.dirname(Config.BASE_DIR)
    target_dir = os.path.join(projects_root, folder_name)
    if not os.path.isdir(target_dir):
        await msg.reply_text(f"Rejected: no sibling project folder named '{folder_name}'.")
        return

    # .env files are tiny; 64 KB is already generous.
    if doc.file_size and doc.file_size > 64 * 1024:
        await msg.reply_text("Rejected: file too large to be a .env (>64 KB).")
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await msg.reply_text(f"Download failed: {e}")
        return

    # Sanity: non-empty, decodable, and contains at least one KEY=value line
    # (guards against the historical 0-byte .env incident).
    try:
        text = data.decode("utf-8-sig")
    except Exception:
        await msg.reply_text("Rejected: file is not readable text.")
        return
    if not re.search(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", text, re.MULTILINE):
        await msg.reply_text("Rejected: no KEY=value lines found - that does not look like a .env.")
        return

    dest = os.path.join(target_dir, ".env")
    backed_up = False
    try:
        if os.path.exists(dest):
            shutil.copy2(dest, dest + ".bak")
            backed_up = True
        part_path = dest + ".part"
        with open(part_path, "wb") as f:
            f.write(data)
        os.replace(part_path, dest)
    except Exception as e:
        await msg.reply_text(f"Failed to write .env: {e}")
        return

    note = " (previous .env saved as .env.bak)" if backed_up else ""
    await msg.reply_text(
        f"Wrote {len(data)} bytes to {folder_name}/.env{note}.\n"
        f"Restart that project via the Admin Hub to apply it."
    )


async def _b2_key_upload(update, context, doc):
    """Bootstrap: receive the B2 application key as a small .json DM'd by an admin
    and store it gitignored at Config.B2_KEY_PATH. Order of checks: admin (done by
    router) -> size -> valid key JSON -> persist atomically."""
    # The key json is tiny; cap at 20 KB.
    if doc.file_size and doc.file_size > 20 * 1024:
        await update.message.reply_text("Rejected: file too large to be a B2 key (>20 KB).")
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return

    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except Exception:
        await update.message.reply_text("Rejected: not valid JSON.")
        return
    if not isinstance(parsed, dict):
        await update.message.reply_text("Rejected: expected a JSON object.")
        return
    key_id = parsed.get("keyID") or parsed.get("applicationKeyId") or parsed.get("key_id")
    app_key = parsed.get("applicationKey") or parsed.get("application_key") or parsed.get("appKey")
    if not (isinstance(key_id, str) and key_id and isinstance(app_key, str) and app_key):
        await update.message.reply_text(
            'Rejected: not a B2 key (need {"keyID": "...", "applicationKey": "..."}).'
        )
        return

    try:
        _store_b2_key(key_id, app_key)
    except Exception as e:
        await update.message.reply_text(f"Failed to store key: {e}")
        return

    await update.message.reply_text("B2 application key stored - cloud transfer layer armed.")


async def cmd_armb2(update, context):
    """Arm the cloud layer from a command: /armb2 <keyID> <applicationKey>.
    The message containing the secret is deleted from the chat after storing."""
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /armb2 <keyID> <applicationKey>")
        return
    try:
        _store_b2_key(context.args[0], context.args[1])
    except Exception as e:
        await update.message.reply_text(f"Failed to store key: {e}")
        return
    try:
        await update.message.delete()  # scrub the key from the chat history
    except Exception:
        pass
    await update.effective_chat.send_message(
        "B2 application key stored - cloud transfer layer armed. "
        "(Your message was deleted to scrub the key.)"
    )


async def cmd_pushdb(update, context):
    """On-demand DB backup: snapshot the live SQLite DB and upload it to B2."""
    if not _is_admin(update):
        return
    import cloud_sync
    if not cloud_sync.is_configured():
        await update.message.reply_text("Cloud layer not armed yet (missing B2 key).")
        return
    await update.message.reply_text("Backing up DB to cloud storage...")
    db_path = os.path.join(Config.BASE_DIR, "data", "bus_data.db")
    # Run off the event loop so a multi-MB upload doesn't freeze the poller.
    result = await asyncio.to_thread(cloud_sync.push_db_backup, db_path)
    if result:
        await update.message.reply_text(f"DB backup uploaded: {result}")
    else:
        await update.message.reply_text("DB backup failed (see server logs).")


async def cmd_pull(update, context):
    """On-demand model deploy: download a B2 object into src/models/ (restores the
    lost /upload_model). Usage: /pull <object_name>."""
    if not _is_admin(update):
        return
    import cloud_sync
    if not cloud_sync.is_configured():
        await update.message.reply_text("Cloud layer not armed yet (missing B2 key).")
        return
    if not context.args:
        await update.message.reply_text("Usage: /pull <object_name>  (downloads into src/models/)")
        return

    object_name = context.args[0]
    # basename() keeps the write inside src/models/ regardless of the object path.
    dest = os.path.join(Config.BASE_DIR, "src", "models", os.path.basename(object_name))
    await update.message.reply_text(f"Pulling {object_name}...")
    ok = await asyncio.to_thread(cloud_sync.pull, object_name, dest)
    if ok:
        await update.message.reply_text(f"Downloaded to src/models/{os.path.basename(object_name)}")
    else:
        await update.message.reply_text("Pull failed (see server logs).")


def start_bot():
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    import logging
    import socket
    logging.basicConfig(level=logging.INFO)
    # httpx logs every Telegram API request at INFO - including the bot token
    # in the URL. Keep it out of the persistent log file.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not found in environment. Please add it to your .env file.")
        return

    # One token = one poller. A second instance would otherwise steal the token and
    # leave both deaf to a silent HTTP 409. Bind a localhost port for the process
    # lifetime; if it's already taken, another poller is live -> bail out. The OS
    # releases the port automatically when this process dies (no lock file to clean).
    global _SINGLE_INSTANCE_LOCK
    _SINGLE_INSTANCE_LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _SINGLE_INSTANCE_LOCK.bind(("127.0.0.1", 47615))
    except OSError:
        print("Error: another ETA bot instance is already running (single-instance guard). Exiting.")
        return

    print("Starting Telegram Bot...")
    app = ApplicationBuilder().token(token).build()
    # Admin-gated handlers MUST be registered before the catch-all: python-telegram-bot
    # runs only the first matching handler per group, so the catch-all would otherwise
    # swallow /pushdb, /pull, /armb2, and document uploads.
    app.add_handler(MessageHandler(filters.Document.ALL, document_upload_handler))
    app.add_handler(CommandHandler("pushdb", cmd_pushdb))
    app.add_handler(CommandHandler("pull", cmd_pull))
    app.add_handler(CommandHandler("armb2", cmd_armb2))
    app.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, bot_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", action="store_true", help="Run as a Telegram bot")
    args = parser.parse_args()
    
    if args.bot:
        import log_tee
        log_tee.setup("telegram_bot")
        start_bot()
    else:
        predict_live_eta()
