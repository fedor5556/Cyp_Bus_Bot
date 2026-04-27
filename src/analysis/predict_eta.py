import os
import sys
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
    allowed_users = os.environ.get("ALLOWED_TELEGRAM_USERS", "FedShved").split(",")
    allowed_users = [u.strip().lstrip("@") for u in allowed_users]

    if user.username not in allowed_users:
        print(f"Unauthorized access attempt from user: {user.username}")
        return

    text = get_prediction_text()
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")

def start_bot():
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    import logging
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not found in environment. Please add it to your .env file.")
        return
        
    print("Starting Telegram Bot...")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, bot_handler))
    app.run_polling()

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", action="store_true", help="Run as a Telegram bot")
    args = parser.parse_args()
    
    if args.bot:
        start_bot()
    else:
        predict_live_eta()
