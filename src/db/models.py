from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Index, inspect, text
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
    """Stores the calculated event when a bus crosses a stop on its route.

    As of multi-stop capture (Phase 3, step 0) one row is logged per stop a bus
    passes, not just the two hard-coded target stops. `method` distinguishes rows
    produced by the new along-route interpolation ('along_route') from the legacy
    2D-haversine rows (NULL), so the empirical-schedule step can account for the
    methodology change.
    """
    __tablename__ = 'stop_events'

    id = Column(Integer, primary_key=True, autoincrement=True)
    vehicle_id = Column(String, index=True)
    trip_id = Column(String, index=True) # AND HERE!
    route_id = Column(String, index=True)
    stop_id = Column(String, index=True)
    stop_sequence = Column(Integer, nullable=True)   # trip-relative order; ML feature
    actual_arrival_time = Column(DateTime)
    scheduled_arrival_time = Column(DateTime, nullable=True)
    delay_seconds = Column(Integer, nullable=True)
    cross_track_m = Column(Float, nullable=True)     # per-crossing quality flag (worst bracketing ping off-route distance)
    method = Column(String, nullable=True)           # 'along_route' for step-0 rows; NULL for legacy 2D rows

    # (trip_id, stop_id) is globally unique: trip_ids rotate every GTFS download
    # so no service_date is needed. This is the hard dedup guarantee for
    # multi-stop capture (the in-code logged-set check is the soft first pass).
    __table_args__ = (
        Index('uq_stop_events_trip_stop', 'trip_id', 'stop_id', unique=True),
    )

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


# Columns added to existing tables after the DB was first populated. create_all
# is additive at the TABLE level only (it never ALTERs an existing table), and it
# is not called by any runtime entrypoint, so every post-launch schema change
# must be applied here. {table: {column: SQL type}}.
_ADDED_COLUMNS = {
    'stop_events': {
        'stop_sequence': 'INTEGER',
        'cross_track_m': 'FLOAT',
        'method': 'VARCHAR',
    },
}


def migrate_db():
    """Idempotently bring an already-populated DB up to the current schema.

    Safe to call on every startup: it only adds missing columns / indexes and is
    a no-op once applied. Called from monitor.py BEFORE the polling loop. Both
    monitor.py and the ETA bot write the same SQLite file, so the connection sets
    a busy_timeout to wait out a concurrent writer rather than erroring.
    """
    engine = get_engine()

    # Fresh DBs (or any missing tables) get created in full.
    Base.metadata.create_all(engine)

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    with engine.begin() as conn:
        # SQLite: don't fail if the other writer holds the lock; wait up to 30 s.
        if engine.dialect.name == 'sqlite':
            conn.execute(text("PRAGMA busy_timeout = 30000"))

        # 1. Add any missing columns.
        for table, cols in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all just built it with every column
            present = {c['name'] for c in insp.get_columns(table)}
            for col, col_type in cols.items():
                if col not in present:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}'))
                    print(f"migrate_db: added {table}.{col} ({col_type})")

        # 2. Hard dedup guarantee for multi-stop capture. The existing rows have
        # unique (trip_id, stop_id) and no nulls, so this builds cleanly.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_stop_events_trip_stop "
            "ON stop_events (trip_id, stop_id)"
        ))

    return engine

def get_session():
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()

if __name__ == "__main__":
    init_db()
