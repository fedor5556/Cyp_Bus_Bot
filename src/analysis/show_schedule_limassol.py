import os
import sys
import pandas as pd

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

def print_schedule():
    stop_id = "5411"  # Panagia Pyrgiotissa Church 2 (Towards Limassol/Lemesos)
    
    data_dir = os.path.join(Config.STATIC_DATA_DIR, 'Limassol')
    stop_times_path = os.path.join(data_dir, 'stop_times.txt')
    trips_path = os.path.join(data_dir, 'trips.txt')
    calendar_dates_path = os.path.join(data_dir, 'calendar_dates.txt')
    
    if not os.path.exists(stop_times_path) or not os.path.exists(trips_path) or not os.path.exists(calendar_dates_path):
        print(f"Error: Could not find all necessary static schedule files at {data_dir}")
        return

    print("Loading timetable data...")
    # Load necessary columns
    df_times = pd.read_csv(stop_times_path, usecols=['trip_id', 'arrival_time', 'stop_id'], dtype=str)
    df_trips = pd.read_csv(trips_path, usecols=['trip_id', 'service_id', 'route_id'], dtype=str)
    df_cal_dates = pd.read_csv(calendar_dates_path, usecols=['service_id', 'date'], dtype=str)
    
    # Map service_ids to days of the week based on dates
    # 0=Monday, 6=Sunday
    df_cal_dates['day_of_week'] = pd.to_datetime(df_cal_dates['date'], format='%Y%m%d').dt.dayofweek
    
    # Classify each service_id into a category
    # If a service runs on days 0-4 it's Weekday, 5 is Saturday, 6 is Sunday
    service_mapping = {}
    for service_id, group in df_cal_dates.groupby('service_id'):
        days = set(group['day_of_week'])
        if any(d in days for d in [0, 1, 2, 3, 4]):
            service_mapping[service_id] = "Weekday (Mon-Fri)"
        elif 5 in days:
            service_mapping[service_id] = "Saturday"
        elif 6 in days:
            service_mapping[service_id] = "Sunday / Holiday"
        else:
            service_mapping[service_id] = "Unknown"

    # Filter for our specific stop and merge
    stop_schedule = df_times[df_times['stop_id'] == stop_id]
    merged = pd.merge(stop_schedule, df_trips, on='trip_id', how='inner')
    
    # Filter for Route 90 (10900011 is Towards Limassol)
    route_schedule = merged[merged['route_id'] == '10900011'].copy()
    
    if route_schedule.empty:
        print()
        print("No scheduled times found for this stop/direction in the active GTFS feed.")
        print()
        return
    
    # Map the service category
    route_schedule['category'] = route_schedule['service_id'].map(service_mapping)
    
    print()
    print("==================================================")
    print("EXACT SCHEDULE: Route 90 -> Towards Limassol")
    print("Stop: Panagia Pyrgiotissa Church (ID: 5411)")
    print("==================================================")
    print()
    
    categories = ["Weekday (Mon-Fri)", "Saturday", "Sunday / Holiday"]
    for cat in categories:
        cat_times = route_schedule[route_schedule['category'] == cat]
        if not cat_times.empty:
            unique_times = sorted(cat_times['arrival_time'].unique())
            print(f"--- {cat} ---")
            for time in unique_times:
                print(f"  * {time}")
            print()

    print("==================================================")

if __name__ == "__main__":
    print_schedule()
