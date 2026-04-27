import os
import sys
import math
from datetime import datetime, timedelta

# Add parent directory to path so we can import config and models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from db.models import VehiclePosition, StopEvent, TripSummary, get_session
from analysis.schedule import get_scheduled_arrival
import pandas as pd

# Geofence parameters for Stop ID 11636 (Panagia Pyrgiotissa Church 1)
STOP_LAT = 34.7416229691767
STOP_LON = 33.1836621951358
RADIUS_METERS = 100 # Consider it "Arrived" if within 100 meters
APPROACH_RADIUS = 400 # Track approaches within 400m
INTERNAL_STOP_ID = "7604"

def haversine(lat1, lon1, lat2, lon2):
    """Calculate the great-circle distance between two points in meters."""
    R = 6371000  # radius of Earth in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = (math.sin(delta_phi / 2) ** 2) + \
        (math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2) ** 2))
    
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def check_geofence(quiet=False):
    session = get_session()
    
    if not quiet:
        print("--------------------------------------------------")
        print(f"Checking Geofence for Stop ID 11636 (Panagia Pyrgiotissa Church 1)")
        print(f"Target Coordinates: ({STOP_LAT}, {STOP_LON}) | Core: {RADIUS_METERS}m | Approach: {APPROACH_RADIUS}m")
        print("--------------------------------------------------\n")
    
    # Get active trips on Route 90 (filter out vehicles that haven't pinged in the last 3 hours)
    three_hours_ago = datetime.now() - timedelta(hours=3)
    active_vehicles = session.query(VehiclePosition.vehicle_id).filter(
        VehiclePosition.route_id.in_(["10900011", "10900012"]),
        VehiclePosition.timestamp >= three_hours_ago
    ).distinct().all()
    
    if not active_vehicles:
        if not quiet:
            print("No active vehicles on Route 90 found in the database. Waiting for activity...")
        session.close()
        return
        
    for (v_id,) in active_vehicles:
        # Get top 2 positions for this vehicle to calculate interpolation and direction
        positions = session.query(VehiclePosition).filter(
            VehiclePosition.vehicle_id == v_id
        ).order_by(VehiclePosition.timestamp.desc()).limit(2).all()
        
        if not positions:
            continue
            
        pos0 = positions[0] # latest
        dist0 = haversine(STOP_LAT, STOP_LON, pos0.latitude, pos0.longitude)
        
        time_since_ping = (datetime.now() - pos0.timestamp).total_seconds()
        is_stale = time_since_ping > 900 or time_since_ping < -900 # 15 minutes
        
        if not quiet:
            direction = "Towards Lemesos" if pos0.route_id == "10900011" else "Towards Sanida"
            status_note = " [STALE]" if is_stale else ""
            stat_note = " [STATIONARY]" if pos0.is_stationary else ""
            print(f"Vehicle {pos0.vehicle_id} ({direction}){status_note}{stat_note}")
            print(f"  Last Ping: {pos0.timestamp} | Pos: ({pos0.latitude:.5f}, {pos0.longitude:.5f})")
            print(f"  Distance to Stop: {dist0:.2f} meters")
            
        if is_stale:
            if not quiet:
                print("  -> Vehicle position is too old. Skipping geofence check.\n")
            continue
            
        pos1 = positions[1] if len(positions) > 1 else None
        dist1 = haversine(STOP_LAT, STOP_LON, pos1.latitude, pos1.longitude) if pos1 else None

        # --- PHASE 2: TERMINAL DELAY TRACKING ---
        if pos0.trip_id:
            today_str = datetime.now().strftime('%Y-%m-%d')
            trip_summary = session.query(TripSummary).filter(
                TripSummary.trip_id == pos0.trip_id,
                TripSummary.date == today_str
            ).first()
            
            if not trip_summary:
                # First time seeing this trip today! Calculate terminal delay.
                stop_times_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stop_times.txt')
                stops_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stops.txt')
                
                try:
                    st_df = pd.read_csv(stop_times_path, dtype=str)
                    trip_stops = st_df[st_df['trip_id'] == pos0.trip_id]
                    
                    if not trip_stops.empty:
                        trip_stops['stop_sequence'] = trip_stops['stop_sequence'].astype(int)
                        terminal_stop_row = trip_stops[trip_stops['stop_sequence'] == trip_stops['stop_sequence'].min()].iloc[0]
                        
                        terminal_stop_id = terminal_stop_row['stop_id']
                        terminal_arrival_time_str = terminal_stop_row['arrival_time']
                        
                        stops_df = pd.read_csv(stops_path, dtype=str)
                        terminal_data = stops_df[stops_df['stop_id'] == terminal_stop_id]
                        
                        if not terminal_data.empty:
                            term_lat = float(terminal_data.iloc[0]['stop_lat'])
                            term_lon = float(terminal_data.iloc[0]['stop_lon'])
                            
                            dist_to_terminal = haversine(term_lat, term_lon, pos0.latitude, pos0.longitude)
                            
                            # If the first ping we see is within 1000m of the terminal, calculate birth delay
                            if dist_to_terminal < 1000:
                                h, m, s = map(int, terminal_arrival_time_str.split(':'))
                                # Handle trips that might go past midnight (e.g. 24:30)
                                if h >= 24:
                                    h -= 24
                                    # Add a day if needed, but for simplicity assuming same day for now
                                base_date = pos0.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
                                terminal_scheduled_dt = base_date + timedelta(hours=h, minutes=m, seconds=s)
                                
                                term_delay_sec = int((pos0.timestamp - terminal_scheduled_dt).total_seconds())
                                
                                new_summary = TripSummary(
                                    trip_id=pos0.trip_id,
                                    route_id=pos0.route_id,
                                    date=today_str,
                                    terminal_departure_delay_seconds=term_delay_sec
                                )
                                session.add(new_summary)
                                session.commit()
                                if not quiet:
                                    print(f"  -> Logged Terminal Delay: {term_delay_sec} seconds for trip {pos0.trip_id}")
                            else:
                                # We missed the terminal start (caught it mid-route). Just create empty summary to not check again.
                                new_summary = TripSummary(
                                    trip_id=pos0.trip_id,
                                    route_id=pos0.route_id,
                                    date=today_str,
                                    terminal_departure_delay_seconds=None
                                )
                                session.add(new_summary)
                                session.commit()
                except Exception as e:
                    if not quiet:
                        print(f"  -> Error calculating terminal delay: {e}")
        # --- END PHASE 2 ---

        # Check if already logged
        recent_event = session.query(StopEvent).filter(
            StopEvent.vehicle_id == pos0.vehicle_id,
            StopEvent.trip_id == pos0.trip_id,
            StopEvent.stop_id.in_(["7604", "5411"])
        ).first()

        if recent_event:
            if not quiet:
                print("  -> Arrival already logged for this trip.\n")
            continue

        arrival_time_to_log = None
        
        # Scenario 1: Entered the 100m zone
        if dist0 <= RADIUS_METERS:
            if pos1 and dist1 and dist1 > RADIUS_METERS:
                # Interpolate exact crossing time
                fraction = (dist1 - RADIUS_METERS) / (dist1 - dist0) if dist1 != dist0 else 0
                time_diff = (pos0.timestamp - pos1.timestamp).total_seconds()
                arrival_time_to_log = pos1.timestamp + timedelta(seconds=fraction * time_diff)
                if not quiet:
                    print(f"  -> >>> ARRIVAL DETECTED! <<< (Interpolated crossing time: {arrival_time_to_log})")
            else:
                # Spawned inside or no previous data
                arrival_time_to_log = pos0.timestamp
                if not quiet:
                    print(f"  -> >>> ARRIVAL DETECTED! <<< (Raw ping time: {arrival_time_to_log})")
                    
        # Scenario 2: Jumped over the zone or missed the 100m core, but passed it
        # If it was within 400m, and is now moving away (dist0 > dist1)
        elif pos1 and dist1 and dist1 <= APPROACH_RADIUS and dist0 > dist1:
            # It was close, but now it's getting further. We missed the core.
            # Log the arrival using the closest known ping (pos1) to prevent silent drops.
            arrival_time_to_log = pos1.timestamp
            if not quiet:
                print(f"  -> >>> PASS-BY DETECTED! <<< Vehicle was at {dist1:.2f}m and is now moving away. Logging arrival at {arrival_time_to_log}.")

        if arrival_time_to_log:
            # Look up the scheduled time!
            scheduled_time = None
            delay_sec = None
            actual_stop_id = INTERNAL_STOP_ID
            
            if pos0.trip_id:
                for stop_id_check in ["7604", "5411"]:
                    scheduled_time = get_scheduled_arrival(pos0.trip_id, stop_id_check, arrival_time_to_log)
                    if scheduled_time:
                        actual_stop_id = stop_id_check
                        break
                        
                if scheduled_time:
                    delay_sec = int((arrival_time_to_log - scheduled_time).total_seconds())
                    delay_min = round(delay_sec / 60, 1)
                    if not quiet:
                        status = "LATE" if delay_sec > 0 else "EARLY"
                        print(f"  -> Scheduled Time: {scheduled_time} (Stop {actual_stop_id})")
                        print(f"  -> Actual Time: {arrival_time_to_log}")
                        print(f"  -> Result: {abs(delay_min)} minutes {status}!")
            
            # Save the final analysis back into the database
            new_event = StopEvent(
                vehicle_id=pos0.vehicle_id,
                trip_id=pos0.trip_id,
                route_id=pos0.route_id,
                stop_id=actual_stop_id,
                actual_arrival_time=arrival_time_to_log,
                scheduled_arrival_time=scheduled_time,
                delay_seconds=delay_sec
            )
            session.add(new_event)
            session.commit()
            
        if not quiet:
            print()
            
    session.close()

if __name__ == "__main__":
    check_geofence()
