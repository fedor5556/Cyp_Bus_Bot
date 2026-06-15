import pandas as pd
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

# --- mtime-cached stop_times index ------------------------------------------
# stop_times.txt is ~6.4 MB. Multi-stop capture (geofence.py) needs the schedule
# for every stop on a trip on every monitor cycle, so re-reading the CSV per
# lookup is wasteful. We load it once into a {trip_id: {stop_id: arrival_str}}
# index and rebuild only when the file's mtime changes (so the 12 h GTFS
# auto-update is picked up). Mirrors the caching strategy in map_matching.py.
_st_cache = {"mtime": None, "by_trip": None}


def _stop_times_path():
    return os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stop_times.txt')


def _load_stop_times():
    """Return the {trip_id: {stop_id: arrival_time_str}} index, or None if the
    file is missing. Cached and invalidated on the file's mtime."""
    path = _stop_times_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _st_cache["mtime"] == mtime and _st_cache["by_trip"] is not None:
        return _st_cache["by_trip"]

    df = pd.read_csv(path, usecols=['trip_id', 'arrival_time', 'stop_id'], dtype=str)
    by_trip = {}
    for r in df.itertuples(index=False):
        by_trip.setdefault(r.trip_id, {})[r.stop_id] = r.arrival_time
    _st_cache["mtime"] = mtime
    _st_cache["by_trip"] = by_trip
    return by_trip


def _parse_gtfs_time(time_str, date):
    """Parse a GTFS HH:MM:SS arrival string into a datetime on `date`. GTFS times
    can exceed 24:00:00 (e.g. 25:30:00 = 01:30 the next day); timedelta rolls the
    overflow into the following day automatically."""
    hours, minutes, seconds = map(int, time_str.split(':'))
    base_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
    return base_date + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def get_scheduled_arrival(trip_id: str, stop_id: str, date: datetime = None):
    """
    Looks up the exact scheduled arrival time for a specific trip at a specific stop.
    Returns a datetime object representing the scheduled time today.
    """
    if date is None:
        date = datetime.now()

    by_trip = _load_stop_times()
    if by_trip is None:
        print(f"Error: Could not find static schedule at {_stop_times_path()}")
        return None

    trip = by_trip.get(str(trip_id))
    if not trip:
        return None
    time_str = trip.get(str(stop_id))
    if time_str is None:
        return None

    try:
        return _parse_gtfs_time(time_str, date)
    except (ValueError, AttributeError):
        return None


def get_scheduled_arrivals_for_trip(trip_id: str, date: datetime = None):
    """Return {stop_id: scheduled_datetime} for EVERY stop on a trip in one shot.

    Used by multi-stop arrival capture so a single cached lookup covers all of a
    trip's ~40 stops instead of one CSV scan per stop. Returns {} if the trip is
    unknown or the schedule file is missing."""
    if date is None:
        date = datetime.now()

    by_trip = _load_stop_times()
    if not by_trip:
        return {}
    trip = by_trip.get(str(trip_id))
    if not trip:
        return {}

    out = {}
    for stop_id, time_str in trip.items():
        try:
            out[stop_id] = _parse_gtfs_time(time_str, date)
        except (ValueError, AttributeError):
            continue
    return out

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
