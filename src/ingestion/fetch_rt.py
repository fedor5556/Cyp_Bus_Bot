import os
import sys
import math
import requests
from google.transit import gtfs_realtime_pb2
from datetime import datetime

# Add parent directories to path so we can import config and models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from db.models import get_session, VehiclePosition

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

def fetch_realtime_data(quiet=False):
    Config.ensure_directories()
    url = Config.GTFS_RT_URL
    if not quiet:
        print(f"Fetching GTFS-RT data from {url}...")
    
    session = get_session()
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)
        
        vehicles_found = 0
        route_90_vehicles = []
        
        # Pre-load Route 90 trip IDs from static data for fast lookup
        data_dir = os.path.join(Config.STATIC_DATA_DIR, 'Limassol')
        trips_path = os.path.join(data_dir, 'trips.txt')
        route_90_trip_ids = set()
        if os.path.exists(trips_path):
            import pandas as pd
            df_trips = pd.read_csv(trips_path, usecols=['trip_id', 'route_id'], dtype=str)
            route_90_trip_ids = set(df_trips[df_trips['route_id'].isin(['10900011', '10900012'])]['trip_id'].unique())

        for entity in feed.entity:
            if entity.HasField('vehicle'):
                vehicles_found += 1
                vehicle = entity.vehicle
                
                # Identify Route 90 by Trip ID or Route ID
                is_route_90 = (vehicle.trip.route_id in ["10900011", "10900012"]) or \
                             (vehicle.trip.trip_id in route_90_trip_ids)
                
                if is_route_90:
                    route_90_vehicles.append(vehicle)
                    
                    # Normalize route_id for database consistency if we matched by trip_id
                    db_route_id = vehicle.trip.route_id
                    if db_route_id not in ["10900011", "10900012"] and vehicle.trip.trip_id in route_90_trip_ids:
                        # Find the actual static route_id for this trip
                        match = df_trips[df_trips['trip_id'] == vehicle.trip.trip_id]
                        if not match.empty:
                            db_route_id = match.iloc[0]['route_id']

                    pos_time = datetime.fromtimestamp(vehicle.timestamp) if vehicle.timestamp else datetime.utcnow()
                    
                    # Check if stationary
                    prev_pos = session.query(VehiclePosition).filter(
                        VehiclePosition.vehicle_id == vehicle.vehicle.id,
                        VehiclePosition.trip_id == vehicle.trip.trip_id
                    ).order_by(VehiclePosition.timestamp.desc()).first()
                    
                    is_stat = False
                    if prev_pos:
                        dist = haversine(prev_pos.latitude, prev_pos.longitude, vehicle.position.latitude, vehicle.position.longitude)
                        if dist < 30.0:
                            is_stat = True
                    
                    # Insert into Database
                    db_record = VehiclePosition(
                        vehicle_id=vehicle.vehicle.id,
                        trip_id=vehicle.trip.trip_id,
                        route_id=db_route_id, # Use normalized ID
                        direction_id=vehicle.trip.direction_id,
                        latitude=vehicle.position.latitude,
                        longitude=vehicle.position.longitude,
                        timestamp=pos_time,
                        current_stop_id=vehicle.stop_id,
                        current_status=vehicle.current_status,
                        is_stationary=is_stat
                    )
                    session.add(db_record)
        
        session.commit()
        
        if not quiet:
            print(f"\n--- Live Tracking Report ---")
            print(f"Total active vehicles: {vehicles_found}")
            print(f"Route 90 vehicles saved to DB: {len(route_90_vehicles)}")
                  
    except Exception as e:
        if not quiet:
            print(f"Error fetching/saving GTFS-RT: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    fetch_realtime_data()
