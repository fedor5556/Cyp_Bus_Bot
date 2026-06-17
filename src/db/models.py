from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Index, inspect, text, event
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

class PredictionLog(Base):
    """Snapshots the bot's own forward ETA to a direction's target stop (Phase 3,
    step 2). One row per active bus per logging tick (~60 s) while it has a genuine
    forward prediction. Joined later to StopEvent on (trip_id, stop_id) to measure
    prediction *drift* (error vs lead time) and calibrate prediction intervals.

    The values are produced by the SAME analysis.eta.compute_vehicle_prediction the
    ETA bot formats, so for any single shared invocation the logged row and the bot's
    text are the same computation. The logger and the live bot invoke it separately
    (different `now`, and the bot does its own reactive GTFS-RT fetch), so a row is not
    literally the render a user saw - it is the identical math on each side's inputs.
    """
    __tablename__ = 'prediction_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    vehicle_id = Column(String, index=True)
    trip_id = Column(String, index=True)
    route_id = Column(String, index=True)
    stop_id = Column(String, index=True)             # the direction's target stop
    predicted_at = Column(DateTime, index=True)      # the 'now' the prediction was made at
    predicted_arrival = Column(DateTime)             # the forecast arrival time
    lead_time_min = Column(Float)                    # (predicted_arrival - predicted_at)/60
    eta_source = Column(String, nullable=True)       # 'schedule' | 'move' | 'hybrid'
    distance_m = Column(Float, nullable=True)        # smoothed along-route distance to stop
    speed_kmh = Column(Float, nullable=True)
    delay_seconds = Column(Integer, nullable=True)   # schedule delay at the closest stop, if known
    is_stationary = Column(Boolean, default=False)
    is_stale = Column(Boolean, default=False)
    on_route = Column(Boolean, default=False)        # bus snapped within the off-route threshold
    # CLOCK CAUTION: recorded_at is UTC (datetime.utcnow, the repo-wide convention
    # for row-insert metadata), but predicted_at / predicted_arrival above are LOCAL
    # (datetime.now() == EEST on the server) because they must line up with StopEvent's
    # local arrival times for the drift join. So the two timestamps in one row are
    # ~3 h apart by design. Analyse drift on predicted_at/predicted_arrival ONLY; never
    # bucket or filter on recorded_at as a proxy for prediction time.
    recorded_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_predlog_trip_stop', 'trip_id', 'stop_id'),
    )

# Process-wide singleton Engine (built lazily, reused for the process lifetime).
_ENGINE = None


def _build_engine():
    db_url = Config.DATABASE_URL
    if not db_url or "user:password" in db_url:
        db_path = os.path.join(Config.BASE_DIR, 'data', 'bus_data.db')
        db_url = f"sqlite:///{db_path}"

    engine = create_engine(db_url, echo=False)

    if engine.dialect.name == "sqlite":
        # Apply the SQLite hardening PRAGMAs to EVERY runtime connection, not just
        # migrate_db's one-off. monitor.py and the ETA bot are two separate
        # processes writing the same bus_data.db file.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            try:
                # busy_timeout is per-connection and never needs a lock -> set it
                # first. Wait up to 30 s for a concurrent writer instead of raising
                # 'database is locked' the instant two writes collide.
                cur.execute("PRAGMA busy_timeout=30000")
                # WAL: readers never block the writer and the writer doesn't block
                # readers, so the two processes collide far less. WAL is a persistent
                # property of the file (set once, stays set across reopens).
                #
                # CAUTION: the ONE-TIME DELETE->WAL conversion needs a brief exclusive
                # lock and does NOT honor busy_timeout -- it raises 'database is locked'
                # *immediately* if the other process (monitor vs bot) holds a write lock
                # at that instant. Swallow that so a startup race can't crash the
                # connection: it proceeds in the file's current (DELETE) mode with
                # busy_timeout still in force, and the next connection that finds the
                # file unlocked performs the conversion. On an already-WAL file this
                # PRAGMA is a no-op that succeeds even under a held write lock, so once
                # converted the race can never recur.
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    pass
            finally:
                cur.close()

    return engine


def get_engine():
    """Return the process-wide singleton Engine, building it on first use.

    Previously this constructed a NEW Engine (and connection pool) on every call,
    and get_session() calls it on every monitor tick (10 s) and every bot query --
    so each tick spun up and tore down a pool/file handle against bus_data.db. An
    Engine is meant to be a long-lived per-process singleton: build once, reuse.
    The connect listener in _build_engine also guarantees busy_timeout + WAL apply
    to the RUNTIME path, which finding #1 noted only migrate_db set before.
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _build_engine()
    return _ENGINE

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
