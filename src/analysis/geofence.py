import os
import sys
import math
from datetime import datetime, timedelta

# Add parent directory to path so we can import config and models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from db.models import VehiclePosition, StopEvent, TripSummary, get_session
from analysis.schedule import get_scheduled_arrivals_for_trip
from analysis import map_matching as mm
from sqlalchemy.exc import IntegrityError
import pandas as pd

# Route 90, both directions (stable route_ids; trip_ids rotate every GTFS pull).
ROUTE_IDS = ["10900011", "10900012"]

# A ping older (or more future) than this is too stale to trust for crossing
# interpolation.
STALE_PING_SECONDS = 900  # 15 minutes

# Primary target stop (11636 / internal 7604, Panagia Pyrgiotissa Church 1) and
# the 100 m "arrived" radius. Multi-stop capture no longer special-cases this
# stop (it's captured like any other), but the ETA bot (predict_eta.py) still
# imports these for its straight-line fallback and "AT CHURCH" check.
STOP_LAT = 34.7416229691767
STOP_LON = 33.1836621951358
RADIUS_METERS = 100


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


def _track_terminal_delay(session, pos0, quiet):
    """PHASE 2: the first time we see a trip near its starting terminal, log its
    'birth delay' (departure delay from the terminal). One TripSummary per trip
    per day; an empty summary is written if we caught the bus mid-route so we
    don't recompute every cycle."""
    if not pos0.trip_id:
        return

    today_str = datetime.now().strftime('%Y-%m-%d')
    trip_summary = session.query(TripSummary).filter(
        TripSummary.trip_id == pos0.trip_id,
        TripSummary.date == today_str
    ).first()
    if trip_summary:
        return

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

                    session.add(TripSummary(
                        trip_id=pos0.trip_id,
                        route_id=pos0.route_id,
                        date=today_str,
                        terminal_departure_delay_seconds=term_delay_sec
                    ))
                    session.commit()
                    if not quiet:
                        print(f"  -> Logged Terminal Delay: {term_delay_sec} seconds for trip {pos0.trip_id}")
                else:
                    # We missed the terminal start (caught it mid-route). Just create empty summary to not check again.
                    session.add(TripSummary(
                        trip_id=pos0.trip_id,
                        route_id=pos0.route_id,
                        date=today_str,
                        terminal_departure_delay_seconds=None
                    ))
                    session.commit()
    except Exception as e:
        session.rollback()
        if not quiet:
            print(f"  -> Error calculating terminal delay: {e}")


def _commit_events(session, events):
    """Commit StopEvents, tolerating a UNIQUE(trip_id, stop_id) collision (a race
    with another writer, or a logic dupe). Returns the count actually persisted.
    On a batch collision we retry row-by-row so one dupe can't drop the rest."""
    try:
        session.add_all(events)
        session.commit()
        return len(events)
    except IntegrityError:
        session.rollback()
        committed = 0
        for e in events:
            try:
                session.add(e)
                session.commit()
                committed += 1
            except IntegrityError:
                session.rollback()
        return committed


def _log_crossings(session, pos0, pos1, quiet):
    """Log a StopEvent for every Route 90 stop the ping pair pos1 (older) -> pos0
    (newer) crossed, using ALONG-ROUTE distance so multiple stops per ping-pair
    are ordered and interpolated correctly. Returns the number of new events.

    A stop at along-route distance `s` is crossed when a1 < s <= a0 (a1/a0 = the
    older/newer ping's along-route distance). Arrival is interpolated assuming
    ~constant along-route speed between the two pings."""
    route_id = pos0.route_id

    # Both pings must be on the same direction's shape for a valid bracket.
    if pos1.route_id != route_id:
        return 0
    # Need a trip_id for the schedule lookup and dedup (skip gracefully if absent).
    if not pos0.trip_id:
        return 0
    # If both pings carry a (different) trip_id the pair straddles a trip change
    # (terminal turnaround) - skip rather than interpolate across it.
    if pos1.trip_id and pos1.trip_id != pos0.trip_id:
        return 0

    shape_id = mm.shape_for_route(route_id)
    if not shape_id:
        return 0
    p0 = mm.project_point(shape_id, pos0.latitude, pos0.longitude)
    p1 = mm.project_point(shape_id, pos1.latitude, pos1.longitude)
    if p0 is None or p1 is None:
        return 0
    a0, cross0 = p0  # newer
    a1, cross1 = p1  # older

    # Guard: off-route pings have unreliable projections.
    if cross0 > mm.OFF_ROUTE_THRESHOLD_M or cross1 > mm.OFF_ROUTE_THRESHOLD_M:
        return 0
    # Guard: no forward along-route progress (stationary / backward GPS jitter).
    if a0 <= a1:
        return 0

    stops = mm.ordered_stops_for_route(route_id)
    if not stops:
        return 0

    # Soft dedup: skip stops already logged for this trip so we don't generate
    # doomed inserts. The UNIQUE index is the hard guarantee.
    logged = {s for (s,) in session.query(StopEvent.stop_id).filter(
        StopEvent.trip_id == pos0.trip_id).all()}

    scheduled_map = get_scheduled_arrivals_for_trip(pos0.trip_id, pos0.timestamp)
    time_diff = (pos0.timestamp - pos1.timestamp).total_seconds()
    span = a0 - a1
    worst_cross = max(cross0, cross1)

    new_events = []
    described = []
    for st in stops:
        s = st["dist_along_m"]
        if not (a1 < s <= a0):
            continue
        stop_id = st["stop_id"]
        if stop_id in logged:
            continue
        # Skip stops that don't snap to this shape (unreliable projection).
        if st["cross_track_m"] > mm.OFF_ROUTE_THRESHOLD_M:
            continue

        frac = (s - a1) / span
        arrival = pos1.timestamp + timedelta(seconds=frac * time_diff)
        scheduled = scheduled_map.get(stop_id)
        delay = int((arrival - scheduled).total_seconds()) if scheduled else None

        new_events.append(StopEvent(
            vehicle_id=pos0.vehicle_id,
            trip_id=pos0.trip_id,
            route_id=route_id,
            stop_id=stop_id,
            stop_sequence=st["stop_sequence"],
            actual_arrival_time=arrival,
            scheduled_arrival_time=scheduled,
            delay_seconds=delay,
            cross_track_m=worst_cross,
            method="along_route",
        ))
        logged.add(stop_id)
        described.append(f"{stop_id}@{arrival.strftime('%H:%M:%S')}"
                         + (f"({delay:+d}s)" if delay is not None else ""))

    if not new_events:
        return 0

    committed = _commit_events(session, new_events)
    if not quiet and committed:
        direction = "Towards Lemesos" if route_id == "10900011" else "Towards Sanida"
        print(f"  -> Logged {committed} crossing(s) [{direction}] trip {pos0.trip_id}: "
              + ", ".join(described))
    return committed


def check_geofence(quiet=False):
    """Scan recently-active Route 90 vehicles and log a StopEvent for every stop
    each one has crossed since its previous ping (multi-stop capture)."""
    session = get_session()

    if not quiet:
        print("--------------------------------------------------")
        print("Checking Route 90 stop crossings (multi-stop, along-route)")
        print("--------------------------------------------------\n")

    # Active vehicles on Route 90 (pinged within the last 3 hours).
    three_hours_ago = datetime.now() - timedelta(hours=3)
    active_vehicles = session.query(VehiclePosition.vehicle_id).filter(
        VehiclePosition.route_id.in_(ROUTE_IDS),
        VehiclePosition.timestamp >= three_hours_ago
    ).distinct().all()

    if not active_vehicles:
        if not quiet:
            print("No active vehicles on Route 90 found in the database. Waiting for activity...")
        session.close()
        return

    total_new = 0
    for (v_id,) in active_vehicles:
        # Top 2 positions for this vehicle (newest first) to bracket crossings.
        positions = session.query(VehiclePosition).filter(
            VehiclePosition.vehicle_id == v_id
        ).order_by(VehiclePosition.timestamp.desc()).limit(2).all()

        if not positions:
            continue

        pos0 = positions[0]  # latest
        time_since_ping = (datetime.now() - pos0.timestamp).total_seconds()
        is_stale = abs(time_since_ping) > STALE_PING_SECONDS

        if not quiet:
            direction = "Towards Lemesos" if pos0.route_id == "10900011" else "Towards Sanida"
            stale_note = " [STALE]" if is_stale else ""
            stat_note = " [STATIONARY]" if pos0.is_stationary else ""
            print(f"Vehicle {pos0.vehicle_id} ({direction}){stale_note}{stat_note}")
            print(f"  Last Ping: {pos0.timestamp} | Pos: ({pos0.latitude:.5f}, {pos0.longitude:.5f})")

        if is_stale:
            if not quiet:
                print("  -> Vehicle position is too old. Skipping.\n")
            continue

        # PHASE 2: terminal departure ("birth") delay, logged once per trip/day.
        _track_terminal_delay(session, pos0, quiet)

        # Need a pair of pings to bracket crossings.
        if len(positions) < 2:
            if not quiet:
                print("  -> Only one ping so far; waiting for a second to bracket crossings.\n")
            continue

        pos1 = positions[1]  # previous
        total_new += _log_crossings(session, pos0, pos1, quiet)

        if not quiet:
            print()

    if not quiet:
        print(f"Cycle complete: {total_new} new stop crossing(s) logged.\n")

    session.close()


if __name__ == "__main__":
    check_geofence()
