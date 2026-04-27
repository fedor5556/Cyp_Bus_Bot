import pandas as pd
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

def get_scheduled_arrival(trip_id: str, stop_id: str, date: datetime = None):
    """
    Looks up the exact scheduled arrival time for a specific trip at a specific stop.
    Returns a datetime object representing the scheduled time today.
    """
    if date is None:
        date = datetime.now()
        
    # We load the static stop_times.txt for Limassol
    stop_times_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stop_times.txt')
    
    if not os.path.exists(stop_times_path):
        print(f"Error: Could not find static schedule at {stop_times_path}")
        return None

    # Load only the necessary columns to save memory
    df = pd.read_csv(stop_times_path, usecols=['trip_id', 'arrival_time', 'stop_id'], dtype=str)
    
    # Filter for the exact trip and stop
    match = df[(df['trip_id'] == trip_id) & (df['stop_id'] == stop_id)]
    
    if match.empty:
        return None
        
    time_str = match.iloc[0]['arrival_time']
    
    # GTFS times can be > 24:00:00 (e.g., 25:30:00 means 01:30 AM the next day)
    hours, minutes, seconds = map(int, time_str.split(':'))
    
    base_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
    scheduled_datetime = base_date + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    
    return scheduled_datetime

def get_next_scheduled_arrival(route_id: str, stop_id: str, current_time: datetime = None):
    """
    Finds the very next scheduled arrival time for a specific route at a specific stop,
    checking today and tomorrow's schedules.
    """
    if current_time is None:
        current_time = datetime.now()
        
    static_dir = os.path.join(Config.STATIC_DATA_DIR, 'Limassol')
    
    # 1. Get dates
    today_str = current_time.strftime('%Y%m%d')
    tomorrow_str = (current_time + timedelta(days=1)).strftime('%Y%m%d')
    
    # 2. Get active services from calendar_dates.txt
    cd_path = os.path.join(static_dir, 'calendar_dates.txt')
    if not os.path.exists(cd_path):
        return None
        
    cd_df = pd.read_csv(cd_path, dtype=str)
    
    today_services = cd_df[(cd_df['date'] == today_str) & (cd_df['exception_type'] == '1')]['service_id'].tolist()
    tomorrow_services = cd_df[(cd_df['date'] == tomorrow_str) & (cd_df['exception_type'] == '1')]['service_id'].tolist()
    
    # 3. Get trips for this route and active services
    trips_path = os.path.join(static_dir, 'trips.txt')
    if not os.path.exists(trips_path):
        return None
        
    trips_df = pd.read_csv(trips_path, usecols=['route_id', 'service_id', 'trip_id'], dtype=str)
    
    today_trips = trips_df[(trips_df['route_id'] == route_id) & (trips_df['service_id'].isin(today_services))]['trip_id'].tolist()
    tomorrow_trips = trips_df[(trips_df['route_id'] == route_id) & (trips_df['service_id'].isin(tomorrow_services))]['trip_id'].tolist()
    
    # 4. Get stop times for these trips at the specific stop
    stop_times_path = os.path.join(static_dir, 'stop_times.txt')
    if not os.path.exists(stop_times_path):
        return None
        
    st_df = pd.read_csv(stop_times_path, usecols=['trip_id', 'arrival_time', 'stop_id'], dtype=str)
    
    # Filter for the specific stop
    st_df = st_df[st_df['stop_id'] == stop_id]
    
    arrivals = []
    
    def process_trips(trip_list, date_obj):
        base_date = date_obj.replace(hour=0, minute=0, second=0, microsecond=0)
        filtered_st = st_df[st_df['trip_id'].isin(trip_list)]
        
        for _, row in filtered_st.iterrows():
            time_str = row['arrival_time']
            hours, minutes, seconds = map(int, time_str.split(':'))
            scheduled_dt = base_date + timedelta(hours=hours, minutes=minutes, seconds=seconds)
            arrivals.append(scheduled_dt)

    process_trips(today_trips, current_time)
    process_trips(tomorrow_trips, current_time + timedelta(days=1))
    
    # 5. Find the next arrival
    future_arrivals = [dt for dt in arrivals if dt > current_time]
    future_arrivals.sort()
    
    if future_arrivals:
        return future_arrivals[0]
    return None

if __name__ == "__main__":
    # Small test: find the arrival time for an arbitrary trip at our church stop (ID 7604)
    # We will just print the first one we find in the CSV as a sanity check.
    test_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stop_times.txt')
    if os.path.exists(test_path):
        df_test = pd.read_csv(test_path, usecols=['trip_id', 'arrival_time', 'stop_id'], dtype=str)
        sample = df_test[df_test['stop_id'] == "7604"].iloc[0]
        print(f"Test Lookup: Trip {sample['trip_id']} is scheduled at {sample['arrival_time']} at stop 7604.")
        
        result = get_scheduled_arrival(sample['trip_id'], "7604")
        print(f"Parsed Datetime Object: {result}")
