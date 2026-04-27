import os
import sys
import pandas as pd
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.models import get_session, StopEvent
from analysis.schedule import get_scheduled_arrival

STOP_IDS = ["7604", "5411"]  # Panagia Pyrgiotissa Church 1 & 2

def recalculate_historical_delays():
    session = get_session()
    events = session.query(StopEvent).filter(StopEvent.delay_seconds == None).all()
    
    fixed_count = 0
    for event in events:
        if not event.trip_id:
            continue
        
        # Try to find the scheduled time in either direction
        for stop_id in STOP_IDS:
            scheduled_time = get_scheduled_arrival(event.trip_id, stop_id, event.actual_arrival_time)
            if scheduled_time:
                delay_sec = int((event.actual_arrival_time - scheduled_time).total_seconds())
                event.scheduled_arrival_time = scheduled_time
                event.delay_seconds = delay_sec
                event.stop_id = stop_id
                fixed_count += 1
                break
    
    session.commit()
    print(f"Recalculated missing delays for {fixed_count} historical events.")
    session.close()

def generate_insights():
    session = get_session()
    events = session.query(StopEvent).filter(StopEvent.delay_seconds != None).all()
    
    if not events:
        print("No delayed events found yet to build a model.")
        return
        
    data = []
    for e in events:
        data.append({
            'stop_id': e.stop_id,
            'direction': "Towards Sanida" if e.stop_id == "5411" else "Towards Lemesos",
            'actual': e.actual_arrival_time,
            'scheduled': e.scheduled_arrival_time,
            'delay_min': e.delay_seconds / 60.0,
            'hour': e.actual_arrival_time.hour
        })
        
    df = pd.DataFrame(data)
    print("")
    print("--- Route 90 Delay Analysis (Stop: Panagia Pyrgiotissa Church) ---")
    
    # Average delay overall
    avg_delay = df['delay_min'].mean()
    status = "LATE" if avg_delay > 0 else "EARLY"
    print("")
    print(f"Overall Average: {abs(avg_delay):.2f} minutes {status}")
    
    # By direction
    print("")
    print("Average Delay by Direction:")
    print(df.groupby('direction')['delay_min'].mean().round(2).apply(lambda x: f"{abs(x)} mins {'LATE' if x > 0 else 'EARLY'}"))
    
    # By hour (Predictive feature)
    print("")
    print("Average Delay by Hour of Day:")
    print(df.groupby('hour')['delay_min'].mean().round(2).apply(lambda x: f"{abs(x)} mins {'LATE' if x > 0 else 'EARLY'}"))
    
    print("")
    print("Recommendation for Buffer Time:")
    max_delay = df['delay_min'].max()
    min_delay = df['delay_min'].min()
    print(f"The bus can arrive up to {abs(min_delay):.1f} minutes EARLY or {max_delay:.1f} minutes LATE.")
    print(f"-> We recommend arriving at the stop {abs(min_delay) + 2:.0f} minutes before the scheduled time.")
    print("-> For a predictive model, we can use the time of day to give a specific expected arrival time.")
    
    session.close()

if __name__ == "__main__":
    print("1. Patching historical missing delays...")
    recalculate_historical_delays()
    print("2. Generating model insights...")
    generate_insights()
