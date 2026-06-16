"""
eta.py - the ETA *calculation*, extracted from the bot (Phase 3, step 2).

Single source of truth. The per-vehicle ETA math used to live inline inside
predict_eta.get_prediction_text(), fused with the Telegram text it built, so there
was no way to obtain a prediction as *data*. That made the PredictionLog (drift)
dataset impossible to produce honestly: any re-implementation of the math in the
logger would drift from what the bot actually showed.

So the calculation lives here and returns a structured VehiclePrediction (no text).
Two consumers call it:
  * the bot (predict_eta.format_prediction) wraps it back into the exact same lines;
  * the logger (monitor.py via log_predictions) writes it into PredictionLog.
One computation, two consumers -> what we log is provably what the bot showed.

`now` is injectable everywhere so tests are deterministic and the bot + logger can
share ONE reference time per tick (the whole point - otherwise the logged arrival
and the shown arrival would differ by the gap between two datetime.now() calls).
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

# Add parent directory to path so we can import config and models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.models import VehiclePosition, PredictionLog
from analysis.geofence import haversine, STOP_LAT, STOP_LON, RADIUS_METERS
from analysis import map_matching as mm
from config import Config

# Route 90 directions (stable route_ids; trip_ids rotate every GTFS pull).
ROUTE_IDS = ["10900011", "10900012"]

# Target stops based on direction.
TARGET_STOPS = {
    "10900012": "7604",  # Towards Sanida (Panagia Pyrgiotissa Church 1)
    "10900011": "5411",  # Towards Lemesos (Panagia Pyrgiotissa Church 2)
}

# A ping older than this (seconds) is "stale": stop extrapolating its movement.
STALE_PING_SECONDS = 120
# SMA speed (m/s) above which the bus counts as "moving" for ETA blending.
MOVING_SPEED_MPS = 1.0


def calculate_sma_speed(session, vehicle_id, current_pos, num_pings=10):
    pings = session.query(VehiclePosition).filter(
        VehiclePosition.vehicle_id == vehicle_id,
        VehiclePosition.timestamp <= current_pos.timestamp
    ).order_by(VehiclePosition.timestamp.desc()).limit(num_pings).all()

    if len(pings) < 2:
        return 0.0

    speeds = []
    for i in range(len(pings) - 1):
        p1, p2 = pings[i], pings[i + 1]
        time_diff = (p1.timestamp - p2.timestamp).total_seconds()
        if time_diff > 0:
            dist = haversine(p1.latitude, p1.longitude, p2.latitude, p2.longitude)
            if dist < 1500:  # Filter GPS noise
                speeds.append(dist / time_diff)

    return sum(speeds) / len(speeds) if speeds else 0.0


def get_trip_state(trip_id, pos):
    """
    Finds the closest stop to the ping, calculates delay, and returns the stop sequence.
    Returns: (delay_sec, current_sequence, target_sequence, target_scheduled_dt)
    """
    stop_times_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stop_times.txt')
    stops_path = os.path.join(Config.STATIC_DATA_DIR, 'Limassol', 'stops.txt')

    if not os.path.exists(stop_times_path) or not os.path.exists(stops_path):
        return 0, -1, -1, None

    # Load trip schedule
    st_df = pd.read_csv(stop_times_path, usecols=['trip_id', 'arrival_time', 'stop_id', 'stop_sequence'], dtype=str)
    trip_df = st_df[st_df['trip_id'] == trip_id].copy()
    if trip_df.empty:
        return 0, -1, -1, None
    trip_df['stop_sequence'] = trip_df['stop_sequence'].astype(int)

    # Identify target stop sequence and schedule for this specific trip
    target_stop_id = TARGET_STOPS.get(pos.route_id)
    if not target_stop_id:
        return 0, -1, -1, None

    target_row = trip_df[trip_df['stop_id'] == target_stop_id]
    if target_row.empty:
        return 0, -1, -1, None

    target_seq = target_row.iloc[0]['stop_sequence']
    target_time_str = target_row.iloc[0]['arrival_time']

    # Base date logic for time parsing
    base_date = pos.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)

    def parse_time(t_str):
        h, m, s = map(int, t_str.split(':'))
        return base_date + timedelta(hours=h, minutes=m, seconds=s)

    target_scheduled_dt = parse_time(target_time_str)

    # Load stops to find closest
    stops_df = pd.read_csv(stops_path, usecols=['stop_id', 'stop_lat', 'stop_lon'], dtype=str)
    merged = pd.merge(trip_df, stops_df, on='stop_id')
    if merged.empty:
        # None of this trip's stop_ids resolved to coordinates in stops.txt (a GTFS
        # integrity gap that can appear right after a schedule refresh). idxmin() on
        # the empty result would raise ValueError, aborting the whole compute. We
        # still have the target stop's schedule, so keep the schedule-backed
        # prediction alive and just skip the closest-stop delay calculation.
        return 0, -1, target_seq, target_scheduled_dt
    merged['stop_lat'] = merged['stop_lat'].astype(float)
    merged['stop_lon'] = merged['stop_lon'].astype(float)

    def calc_dist(row):
        return haversine(pos.latitude, pos.longitude, row['stop_lat'], row['stop_lon'])

    merged['dist'] = merged.apply(calc_dist, axis=1)
    closest = merged.loc[merged['dist'].idxmin()]

    current_seq = closest['stop_sequence']
    sched_dt = parse_time(closest['arrival_time'])
    delay_sec = (pos.timestamp - sched_dt).total_seconds()

    return delay_sec, current_seq, target_seq, target_scheduled_dt


def active_route90_positions(session, now=None):
    """Latest ping per active Route 90 vehicle, newest-first.

    Replicates the bot's original selection exactly: a 15-minute window anchored to
    the later of `now` and the newest ping in the DB (so a timezone skew between the
    server clock and the feed's timestamps can't hide live buses), then deduped to
    the single newest ping per vehicle_id. Shared by the bot and the logger so both
    see the same set of vehicles.
    """
    if now is None:
        now = datetime.now()

    max_db_pos = session.query(VehiclePosition).order_by(VehiclePosition.timestamp.desc()).first()
    max_db_time = max_db_pos.timestamp if max_db_pos else now
    # Anchor the window to the later of `now` and the newest ping so a server-vs-feed
    # timezone offset can't hide live buses -- but CLAMP how far the future may pull
    # it. A single ping with a wildly future timestamp (one vehicle's clock skew)
    # would otherwise shove the threshold ahead of every genuinely-live ping and
    # report "No active vehicles". 2 min still absorbs a real clock offset.
    effective_now = min(max(now, max_db_time), now + timedelta(minutes=2))
    threshold = effective_now - timedelta(minutes=15)

    latest_positions = session.query(VehiclePosition).filter(
        VehiclePosition.route_id.in_(ROUTE_IDS),
        VehiclePosition.timestamp >= threshold
    ).order_by(VehiclePosition.timestamp.desc()).all()

    seen = {}
    for pos in latest_positions:
        if pos.vehicle_id not in seen:
            seen[pos.vehicle_id] = pos
    return list(seen.values())


@dataclass
class VehiclePrediction:
    """The ETA computation for one vehicle, as data. The bot formats it; the logger
    persists the forward ones. `status` selects the formatter branch:
      'at'          - within RADIUS_METERS of the church (no ETA)
      'passed'      - already passed the target this trip (ETA = return-trip guess)
      'scheduled'   - has timetable data -> schedule/hybrid blend
      'no_schedule' - no timetable -> movement-only ETA, or 'Unknown' when not moving
    """
    vehicle_id: str
    route_id: str
    direction: str
    status: str
    predicted_at: datetime              # the reference 'now'
    status_tag: str = ""                # "" | " [STALE DATA]" | " [STATIONARY]"
    trip_id: Optional[str] = None
    stop_id: Optional[str] = None       # the direction's target stop
    # distance / speed
    smooth_distance_m: float = 0.0
    speed_mps: float = 0.0
    speed_kmh: float = 0.0
    moving: bool = False                # speed_mps > MOVING_SPEED_MPS
    on_route: bool = False
    dist_tag: str = ""                  # "" | " [straight-line]"
    is_stale: bool = False
    is_stationary: bool = False
    # schedule
    has_schedule: bool = False
    target_scheduled: Optional[datetime] = None
    status_str: Optional[str] = None    # "Running ... LATE" etc. (scheduled only)
    delay_seconds: Optional[int] = None
    # outcome
    has_passed: bool = False
    eta_minutes: Optional[float] = None
    predicted_arrival: Optional[datetime] = None   # forward arrival; None for at/passed/unknown
    eta_source: Optional[str] = None    # 'schedule' | 'move' | 'hybrid'


def compute_vehicle_prediction(session, pos, now=None):
    """The bot's per-vehicle ETA logic, verbatim, returning a VehiclePrediction
    instead of text. Returns None for a vehicle the bot would skip entirely (no
    schedule data AND stale/stationary). `now` is injectable for deterministic tests.
    """
    if now is None:
        now = datetime.now()

    delay_sec, current_seq, target_seq, target_sched = get_trip_state(pos.trip_id, pos)

    time_since_ping = (now - pos.timestamp).total_seconds()
    is_stale = time_since_ping > STALE_PING_SECONDS
    is_stationary = bool(getattr(pos, 'is_stationary', False))

    # Schedule safeguard: a vehicle with no schedule AND stale/stationary is not shown.
    if not target_sched and (is_stale or is_stationary):
        return None

    raw_sma_speed_mps = calculate_sma_speed(session, pos.vehicle_id, pos, num_pings=10)

    # Halt extrapolation for stale / stationary pings.
    if is_stale or is_stationary:
        sma_speed_mps = 0.0
        extrapolate_time = 0.0
    else:
        sma_speed_mps = raw_sma_speed_mps
        extrapolate_time = max(0, time_since_ping)

    sma_speed_kmh = sma_speed_mps * 3.6
    estimated_movement = sma_speed_mps * extrapolate_time

    # Distance to this direction's target stop measured ALONG THE ROUTE
    # (map-matching), falling back to straight-line haversine off-route.
    target_stop_id = TARGET_STOPS.get(pos.route_id)
    route_dist = (mm.route_distance_to_stop(pos.route_id, pos.latitude, pos.longitude, target_stop_id)
                  if target_stop_id else {"ok": False})
    if route_dist.get("ok") and route_dist.get("on_route"):
        raw_distance = route_dist["abs_distance_m"]
        passed_by_route = route_dist["passed"]
        on_route = True
        dist_tag = ""
    else:
        # Straight-line fallback. Measure to THIS direction's target stop, not a
        # single hard-coded church coordinate: STOP_LAT/STOP_LON is stop 7604
        # (Sanida side), ~57 m from stop 5411 (Lemesos side), so the old code
        # mismeasured every off-route Lemesos-direction row and biased that
        # direction's drift dataset. Fall back to STOP_LAT/STOP_LON only if the
        # stop isn't in stops.txt (e.g. mid-refresh GTFS gap).
        target_coords = mm.stop_coordinates(target_stop_id) if target_stop_id else None
        if target_coords is None:
            target_coords = (STOP_LAT, STOP_LON)
        raw_distance = haversine(target_coords[0], target_coords[1], pos.latitude, pos.longitude)
        passed_by_route = None
        on_route = False
        dist_tag = " [straight-line]"
    # Cap dead-reckoning so it can close at most 80% of the *measured* remaining
    # distance. Otherwise a not-yet-stale ping (<120 s) with a high SMA speed can make
    # estimated_movement exceed raw_distance, collapsing smooth_distance to 0 and
    # tripping a false 'AT' hundreds of metres out -- which also drops the
    # highest-value final-approach rows from PredictionLog (it excludes 'at').
    smooth_distance = max(0, raw_distance - min(estimated_movement, raw_distance * 0.8))

    direction = "Towards Lemesos" if pos.route_id == "10900011" else "Towards Sanida"
    status_tag = " [STALE DATA]" if is_stale else (" [STATIONARY]" if is_stationary else "")
    moving = sma_speed_mps > MOVING_SPEED_MPS

    base = dict(
        vehicle_id=pos.vehicle_id, route_id=pos.route_id, direction=direction,
        predicted_at=now, status_tag=status_tag, trip_id=pos.trip_id, stop_id=target_stop_id,
        smooth_distance_m=smooth_distance, speed_mps=sma_speed_mps, speed_kmh=sma_speed_kmh,
        moving=moving, on_route=on_route, dist_tag=dist_tag,
        is_stale=is_stale, is_stationary=is_stationary,
    )

    # 0. AT THE STOP. Require the *measured* distance to be within radius too, so a
    # bus is never declared 'AT' on extrapolation alone. A bus physically 100-500 m
    # out then stays in a forward-ETA state and its near-arrival prediction is logged
    # instead of being discarded as 'at'. Deliberate specificity tradeoff: a bus truly
    # at the stop but whose last ping is GPS-noisy beyond 100 m shows a near-zero ETA
    # rather than 'AT' (rare in open sky; geofence.py is the authoritative crossing log).
    if smooth_distance <= RADIUS_METERS and raw_distance <= RADIUS_METERS:
        return VehiclePrediction(status="at", **base)

    # 1. ALREADY PASSED (prefer along-route position; fall back to stop-sequence)
    has_passed = passed_by_route if passed_by_route is not None else (target_seq != -1 and current_seq > target_seq)
    if has_passed:
        if moving:
            eta = (smooth_distance / sma_speed_mps) / 60
        else:
            # Terminal wait fallback: assume 30 km/h (8.33 m/s) return speed.
            eta = (smooth_distance / 8.33) / 60
        return VehiclePrediction(status="passed", has_passed=True, eta_minutes=eta,
                                 eta_source="move", **base)

    # 2. SCHEDULE-BACKED
    if target_sched:
        delay_min = delay_sec / 60
        if delay_min > 1:
            status_str = f"Running {int(abs(delay_min))} mins LATE"
        elif delay_min < -15:
            status_str = f"Parked / Waiting for scheduled departure ({int(abs(delay_min))} mins until active)"
        elif delay_min < -1:
            status_str = f"Running {int(abs(delay_min))} mins EARLY"
        else:
            status_str = "Running ON TIME"

        projected_arrival = target_sched + timedelta(seconds=delay_sec)
        profile_eta_min = (projected_arrival - now).total_seconds() / 60

        if moving:
            move_eta_min = (smooth_distance / sma_speed_mps) / 60
            weight_move = max(0, min(1, 1 - (smooth_distance / 12000)))
            hybrid_eta = (move_eta_min * weight_move * 0.8) + (profile_eta_min * (1 - (weight_move * 0.8)))
            if hybrid_eta > 0:
                eta_minutes = hybrid_eta
                eta_source = "hybrid"
            else:
                # The schedule profile has gone non-positive: the bus is late enough that
                # its projected arrival is already in the past, yet it is still moving and
                # (far out, weight_move ~ 0) the blend collapses to that negative profile.
                # Clamping to 0 would show/log a fake-imminent arrival for a bus that may
                # be 15 km away. The movement-based estimate is the only sane forward signal
                # left, so fall back to it (always > 0 here: smooth_distance > RADIUS and
                # sma_speed_mps > MOVING_SPEED_MPS). eta_source records which signal we used.
                eta_minutes = move_eta_min
                eta_source = "move"
            expected_time = now + timedelta(minutes=eta_minutes)
        else:
            eta_minutes = max(0, profile_eta_min)
            expected_time = now + timedelta(minutes=eta_minutes)
            eta_source = "schedule"

        return VehiclePrediction(
            status="scheduled", has_schedule=True, target_scheduled=target_sched,
            status_str=status_str, delay_seconds=int(delay_sec),
            eta_minutes=eta_minutes, predicted_arrival=expected_time, eta_source=eta_source,
            **base)

    # 3. NO SCHEDULE
    if moving:
        eta = (smooth_distance / sma_speed_mps) / 60
        return VehiclePrediction(status="no_schedule", eta_minutes=eta,
                                 predicted_arrival=now + timedelta(minutes=eta),
                                 eta_source="move", **base)
    # Not moving, no schedule -> "Unknown" (no forward ETA, not logged).
    return VehiclePrediction(status="no_schedule", **base)


def log_predictions(session, now=None):
    """Insert one PredictionLog per active Route 90 bus that has a genuine FORWARD
    ETA to its direction's target stop. Skips AT / passed / 'unknown' (no forward
    arrival). Reuses the pings already in the DB - does NOT fetch GTFS-RT. Commits
    each row individually (per-vehicle isolation: one bad vehicle can't roll back the
    others). Returns the number of rows logged. Caller wraps this in try/except.
    """
    if now is None:
        now = datetime.now()

    positions = active_route90_positions(session, now=now)
    logged = 0
    for pos in positions:
        try:
            pred = compute_vehicle_prediction(session, pos, now=now)
            if pred is None:
                continue
            # Only log genuine forward predictions (excludes at / passed / unknown).
            if pred.predicted_arrival is None or pred.has_passed:
                continue

            lead = (pred.predicted_arrival - pred.predicted_at).total_seconds() / 60.0
            session.add(PredictionLog(
                vehicle_id=pred.vehicle_id,
                trip_id=pred.trip_id,
                route_id=pred.route_id,
                stop_id=pred.stop_id,
                predicted_at=pred.predicted_at,
                predicted_arrival=pred.predicted_arrival,
                lead_time_min=lead,
                eta_source=pred.eta_source,
                distance_m=pred.smooth_distance_m,
                speed_kmh=pred.speed_kmh,
                delay_seconds=pred.delay_seconds,
                is_stationary=pred.is_stationary,
                is_stale=pred.is_stale,
                on_route=pred.on_route,
            ))
            session.commit()
            logged += 1
        except Exception as e:
            # Per-vehicle isolation. Commit each row as it is produced and roll back on
            # failure, so one bad vehicle can neither abort the tick nor undo the rows
            # already persisted for the healthy ones. The rollback also clears a session
            # left in a needs-rollback state by a DBAPIError raised inside compute (e.g.
            # an SQLite 'database is locked' during the SMA query), which would otherwise
            # make every subsequent commit fail. A bare except is deliberate: any
            # compute/DB error for one bus must not take down the others.
            session.rollback()
            print(f"log_predictions: skipping vehicle {getattr(pos, 'vehicle_id', '?')}: {e}")
            continue

    return logged
