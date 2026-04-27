from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os
import sys

# Add parent directory to path so we can import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

Base = declarative_base()

class VehiclePosition(Base):
    """Stores the historical GPS pings of the buses."""
    __tablename__ = 'vehicle_positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    vehicle_id = Column(String, index=True)
    trip_id = Column(String, index=True) # THIS IS NEW AND CRITICAL!
    route_id = Column(String, index=True)
    direction_id = Column(Integer, nullable=True)
    latitude = Column(Float)
    longitude = Column(Float)
    timestamp = Column(DateTime, index=True)
    current_stop_id = Column(String, nullable=True)
    current_status = Column(Integer, nullable=True)
    is_stationary = Column(Boolean, default=False)
    
    # Metadata for tracking
    recorded_at = Column(DateTime, default=datetime.utcnow)

class StopEvent(Base):
    """Stores the calculated event when a bus enters the geofence of a stop."""
    __tablename__ = 'stop_events'

    id = Column(Integer, primary_key=True, autoincrement=True)
    vehicle_id = Column(String, index=True)
    trip_id = Column(String, index=True) # AND HERE!
    route_id = Column(String, index=True)
    stop_id = Column(String, index=True)
    actual_arrival_time = Column(DateTime)
    scheduled_arrival_time = Column(DateTime, nullable=True)
    delay_seconds = Column(Integer, nullable=True)

class ScheduleVersion(Base):
    """Tracks updates to the static GTFS files."""
    __tablename__ = 'schedule_versions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(String, index=True)
    download_time = Column(DateTime, default=datetime.utcnow)
    file_hash = Column(String, index=True)
    
class WeatherRecord(Base):
    """Stores hourly or periodic weather data from Open-Meteo."""
    __tablename__ = 'weather_records'

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, index=True)
    temperature_c = Column(Float)
    precipitation_mm = Column(Float)
    wind_speed_kmh = Column(Float)
    is_raining = Column(Boolean, default=False)
    
class TripSummary(Base):
    """Stores route-level metrics for a specific trip, like departure delay."""
    __tablename__ = 'trip_summaries'

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(String, index=True)
    route_id = Column(String, index=True)
    date = Column(String, index=True) # e.g., 'YYYY-MM-DD'
    terminal_departure_delay_seconds = Column(Integer, nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow)

def get_engine():
    db_url = Config.DATABASE_URL
    if not db_url or "user:password" in db_url:
        db_path = os.path.join(Config.BASE_DIR, 'data', 'bus_data.db')
        db_url = f"sqlite:///{db_path}"
    
    engine = create_engine(db_url, echo=False)
    return engine

def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("Database tables with trip_id initialized successfully.")
    return engine

def get_session():
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()

if __name__ == "__main__":
    init_db()
