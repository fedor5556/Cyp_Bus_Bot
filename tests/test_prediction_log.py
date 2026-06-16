"""
Verification for PredictionLog / ETA-drift logging (Phase 3, step 2).

Run directly:  python tests/test_prediction_log.py
Exit code 0 = all checks passed, 1 = a check failed.

Sections:
  1. Branch unit tests - eta.compute_vehicle_prediction, fixed injected `now`,
     the three GTFS/geometry helpers stubbed so every branch is deterministic:
     at / passed(moving) / passed(stopped) / scheduled(hybrid) / scheduled(stationary)
     / no_schedule(moving) / no_schedule(unknown) / skipped(None) / straight-line.
     Asserts status, eta_source, predicted_arrival, has_passed.
  2. Formatter golden - predict_eta.format_prediction over hand-built predictions
     must reproduce the bot's exact lines (locks the templates byte-for-byte), plus
     a get_prediction_text() end-to-end assembly golden (header + per-vehicle join).
  3. Logger - in-memory DB + synthetic active buses through the REAL
     active_route90_positions; eta.log_predictions writes one row per genuine
     forward prediction (skips at / passed / unknown), with the right field mapping
     and lead_time_min == (predicted_arrival - predicted_at)/60.
  4. Migration - migrate_db() on a COPY of the live DB creates prediction_logs +
     its index, is idempotent, and leaves existing rows untouched.
"""

import os
import sys
import shutil
import sqlite3
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

# Redirect the engine at a throwaway DB BEFORE importing config/models, so the
# migration section (the only code that calls get_engine) can never touch the live
# DB. config reads DATABASE_URL at import time, so this must come first.
LIVE_DB = os.path.join(ROOT, "data", "bus_data.db")
COPY_DB = os.path.join(ROOT, "data", "_predlog_migtest.db")
HAVE_LIVE = os.path.exists(LIVE_DB)
# Clear any stale copy + WAL/SHM sidecars from a crashed previous run BEFORE copying:
# the engine now runs in WAL mode, and a leftover -wal would be replayed onto the
# fresh copy and corrupt it.
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(COPY_DB + _suffix)
    except OSError:
        pass
if HAVE_LIVE:
    shutil.copy2(LIVE_DB, COPY_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + COPY_DB.replace("\\", "/")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, PredictionLog, VehiclePosition, migrate_db  # noqa: E402
from analysis import eta  # noqa: E402
from analysis import predict_eta  # noqa: E402

_failures = []
NOW = datetime(2026, 6, 16, 12, 0, 0)
PING_TS = NOW - timedelta(seconds=30)  # 30s old -> not stale


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


class Pos:
    """Minimal VehiclePosition stand-in for stubbed compute_vehicle_prediction tests."""
    def __init__(self, vehicle_id, route_id, trip_id, lat, lon, ts, is_stationary=False):
        self.vehicle_id = vehicle_id
        self.route_id = route_id
        self.trip_id = trip_id
        self.latitude = lat
        self.longitude = lon
        self.timestamp = ts
        self.is_stationary = is_stationary


# --- patch/restore helpers for the stubbed GTFS/geometry deps -----------------
_ORIG = {}


def stub(trip_state, speed_mps, route_dist):
    """Stub eta's three heavy deps for one scenario."""
    _ORIG.setdefault("get_trip_state", eta.get_trip_state)
    _ORIG.setdefault("calculate_sma_speed", eta.calculate_sma_speed)
    _ORIG.setdefault("mm_rd", eta.mm.route_distance_to_stop)
    eta.get_trip_state = lambda trip_id, pos: trip_state
    eta.calculate_sma_speed = lambda session, vid, pos, num_pings=10: speed_mps
    eta.mm.route_distance_to_stop = lambda route_id, lat, lon, stop_id: route_dist


def restore():
    if "get_trip_state" in _ORIG:
        eta.get_trip_state = _ORIG["get_trip_state"]
        eta.calculate_sma_speed = _ORIG["calculate_sma_speed"]
        eta.mm.route_distance_to_stop = _ORIG["mm_rd"]
    _ORIG.clear()


def on_route(abs_m, passed):
    return {"ok": True, "on_route": True, "abs_distance_m": abs_m, "passed": passed}


SANIDA = "10900012"   # -> "Towards Sanida"
LEMESOS = "10900011"  # -> "Towards Lemesos"


def compute(route_id=SANIDA, trip_id="T1", is_stationary=False, lat=34.0, lon=33.0):
    pos = Pos("V", route_id, trip_id, lat, lon, PING_TS, is_stationary)
    return eta.compute_vehicle_prediction(session=None, pos=pos, now=NOW)


print("1. Branch unit tests (compute_vehicle_prediction, fixed now, stubbed deps)")

# A. AT: distance under the 100m radius.
stub((-120, 5, 10, NOW), 0.0, on_route(50, False))
p = compute()
check("AT -> status 'at', no forward arrival",
      p.status == "at" and p.predicted_arrival is None and not p.has_passed, f"{p.status}")

# B. passed, moving.
stub((0, 12, 10, NOW), 10.0, on_route(2000, True))
p = compute()
check("passed+moving -> has_passed, eta_source 'move', no forward arrival",
      p.status == "passed" and p.has_passed and p.eta_source == "move"
      and p.predicted_arrival is None and approx(p.eta_minutes, (1700 / 10) / 60),
      f"status={p.status} eta_min={p.eta_minutes}")

# C. passed, stopped -> 8.33 m/s terminal fallback.
stub((0, 12, 10, NOW), 0.0, on_route(3000, True))
p = compute()
check("passed+stopped -> 30km/h fallback eta",
      p.status == "passed" and not p.moving and approx(p.eta_minutes, (3000 / 8.33) / 60),
      f"eta_min={p.eta_minutes}")

# D. scheduled, moving -> hybrid.
sched = datetime(2026, 6, 16, 12, 10, 0)
stub((120, 3, 10, sched), 8.0, on_route(5000, False))
p = compute()
check("scheduled+moving -> eta_source 'hybrid', forward arrival, delay logged",
      p.status == "scheduled" and p.eta_source == "hybrid"
      and p.predicted_arrival is not None and p.delay_seconds == 120
      and p.status_str == "Running 2 mins LATE",
      f"src={p.eta_source} status_str={p.status_str!r}")

# E. scheduled, stationary -> schedule profile only (clean numbers).
stub((-300, 8, 10, sched), 0.0, on_route(5000, False))
p = compute(is_stationary=False)  # speed 0 but not flagged: profile path
check("scheduled+stopped -> eta_source 'schedule', arrival == 12:05:00, EARLY",
      p.status == "scheduled" and p.eta_source == "schedule"
      and p.predicted_arrival == datetime(2026, 6, 16, 12, 5, 0)
      and approx(p.eta_minutes, 5.0) and p.status_str == "Running 5 mins EARLY",
      f"src={p.eta_source} arr={p.predicted_arrival} status_str={p.status_str!r}")

# F. no schedule, moving -> movement ETA, forward arrival, delay None.
stub((0, -1, -1, None), 6.0, on_route(4000, False))
p = compute()
eta_f = (3820 / 6) / 60  # smooth = 4000 - 6*30
check("no_schedule+moving -> eta_source 'move', forward arrival, delay None",
      p.status == "no_schedule" and p.eta_source == "move"
      and p.predicted_arrival == NOW + timedelta(minutes=p.eta_minutes)
      and approx(p.eta_minutes, eta_f) and p.delay_seconds is None,
      f"src={p.eta_source} eta_min={p.eta_minutes}")

# G. no schedule, not moving (but not stale/stationary) -> Unknown, no arrival.
stub((0, -1, -1, None), 0.5, on_route(4000, False))
p = compute()
check("no_schedule+slow -> 'Unknown', no forward arrival",
      p.status == "no_schedule" and p.predicted_arrival is None
      and p.eta_minutes is None and p.eta_source is None, f"{p.status}")

# H. no schedule AND stationary -> vehicle skipped entirely (None).
stub((0, -1, -1, None), 0.0, on_route(4000, False))
p = compute(is_stationary=True)
check("no_schedule+stationary -> compute returns None (skipped)", p is None, f"{p}")

# I. off-route -> straight-line fallback (haversine), on_route False, tag set.
stub((0, -1, -1, None), 5.0, {"ok": False})
p = compute(lat=34.7416229691767 + 0.05, lon=33.1836621951358)
check("off-route -> on_route False and ' [straight-line]' tag",
      p is not None and p.on_route is False and p.dist_tag == " [straight-line]",
      f"on_route={getattr(p, 'on_route', None)} tag={getattr(p, 'dist_tag', None)!r}")

restore()


print("2. Formatter golden (locks the bot's exact text templates)")
VP = eta.VehiclePrediction


def golden(p, expected_lines):
    got = predict_eta.format_prediction(p)
    return got == expected_lines, got


# at
p = VP(vehicle_id="V1", route_id=SANIDA, direction="Towards Sanida", status="at",
       predicted_at=NOW)
ok, got = golden(p, [
    "Vehicle V1 (Towards Sanida)",
    "  Status: AT PYRGOS CHURCH",
    "-" * 50,
])
check("format: AT", ok, "" if ok else f"got {got}")

# passed, moving, stale tag, straight-line tag
p = VP(vehicle_id="V2", route_id=LEMESOS, direction="Towards Lemesos", status="passed",
       predicted_at=NOW, status_tag=" [STALE DATA]", smooth_distance_m=1700.0,
       speed_kmh=36.0, moving=True, dist_tag=" [straight-line]", has_passed=True,
       eta_minutes=2.8, eta_source="move")
ok, got = golden(p, [
    "Vehicle V2 (Towards Lemesos) [STALE DATA]",
    "  Distance to Pyrgos Church: 1.7 km (Speed: 36.0 km/h) [straight-line]",
    "  Status: Passed Pyrgos on current trip (At or heading to terminal)",
    "  --> Next ETA to Pyrgos: ~2.8 minutes (Movement-based)",
    "-" * 50,
])
check("format: passed+moving (+tags)", ok, "" if ok else f"got {got}")

# passed, stopped
p = VP(vehicle_id="V3", route_id=SANIDA, direction="Towards Sanida", status="passed",
       predicted_at=NOW, smooth_distance_m=3000.0, speed_kmh=0.0, moving=False,
       has_passed=True, eta_minutes=6.0, eta_source="move")
ok, got = golden(p, [
    "Vehicle V3 (Towards Sanida)",
    "  Distance to Pyrgos Church: 3.0 km (Speed: 0.0 km/h)",
    "  Status: Passed Pyrgos on current trip (At or heading to terminal)",
    "  --> Next ETA to Pyrgos: ~6.0 minutes (Assuming 30km/h once moving)",
    "-" * 50,
])
check("format: passed+stopped", ok, "" if ok else f"got {got}")

# scheduled, moving (hybrid)
p = VP(vehicle_id="V4", route_id=SANIDA, direction="Towards Sanida", status="scheduled",
       predicted_at=NOW, smooth_distance_m=4760.0, speed_kmh=28.8, moving=True,
       has_schedule=True, target_scheduled=datetime(2026, 6, 16, 12, 10, 0),
       status_str="Running 2 mins LATE", delay_seconds=120, eta_minutes=11.0,
       predicted_arrival=datetime(2026, 6, 16, 12, 11, 0), eta_source="hybrid")
ok, got = golden(p, [
    "Vehicle V4 (Towards Sanida)",
    "  Distance to Pyrgos Church: 4.8 km (Speed: 28.8 km/h)",
    "  Timetable Schedule: 12:10:00",
    "  Current Status: Running 2 mins LATE",
    "  --> EXPECTED ARRIVAL: 12:11:00 (in ~11.0 minutes)",
    "-" * 50,
])
check("format: scheduled+moving (hybrid)", ok, "" if ok else f"got {got}")

# scheduled, stationary
p = VP(vehicle_id="V5", route_id=LEMESOS, direction="Towards Lemesos", status="scheduled",
       predicted_at=NOW, status_tag=" [STATIONARY]", smooth_distance_m=5000.0,
       speed_kmh=0.0, moving=False, has_schedule=True,
       target_scheduled=datetime(2026, 6, 16, 12, 10, 0),
       status_str="Running 5 mins EARLY", delay_seconds=-300, eta_minutes=5.0,
       predicted_arrival=datetime(2026, 6, 16, 12, 5, 0), eta_source="schedule")
ok, got = golden(p, [
    "Vehicle V5 (Towards Lemesos) [STATIONARY]",
    "  Distance to Pyrgos Church: 5.0 km (Speed: 0.0 km/h)",
    "  Timetable Schedule: 12:10:00",
    "  Current Status: Running 5 mins EARLY",
    "  --> EXPECTED ARRIVAL: 12:05:00 (in ~5.0 minutes) [Stationary]",
    "-" * 50,
])
check("format: scheduled+stationary", ok, "" if ok else f"got {got}")

# no_schedule, moving
p = VP(vehicle_id="V6", route_id=SANIDA, direction="Towards Sanida", status="no_schedule",
       predicted_at=NOW, smooth_distance_m=3820.0, speed_kmh=21.6, moving=True,
       eta_minutes=10.6, predicted_arrival=NOW + timedelta(minutes=10.6), eta_source="move")
ok, got = golden(p, [
    "Vehicle V6 (Towards Sanida)",
    "  Distance to Pyrgos Church: 3.8 km (Speed: 21.6 km/h)",
    "  --> Predicted ETA: ~10.6 minutes (No schedule data)",
    "-" * 50,
])
check("format: no_schedule+moving", ok, "" if ok else f"got {got}")

# no_schedule, not moving (Unknown)
p = VP(vehicle_id="V7", route_id=SANIDA, direction="Towards Sanida", status="no_schedule",
       predicted_at=NOW, smooth_distance_m=4000.0, speed_kmh=1.8, moving=False)
ok, got = golden(p, [
    "Vehicle V7 (Towards Sanida)",
    "  Distance to Pyrgos Church: 4.0 km (Speed: 1.8 km/h)",
    "  --> Status: Unknown (No schedule, not moving)",
    "-" * 50,
])
check("format: no_schedule+unknown", ok, "" if ok else f"got {got}")

# End-to-end assembly golden for get_prediction_text (header + per-vehicle join).
p_at = VP(vehicle_id="V1", route_id=SANIDA, direction="Towards Sanida", status="at",
          predicted_at=NOW)
p_sched = VP(vehicle_id="V4", route_id=SANIDA, direction="Towards Sanida", status="scheduled",
             predicted_at=NOW, smooth_distance_m=4760.0, speed_kmh=28.8, moving=True,
             has_schedule=True, target_scheduled=datetime(2026, 6, 16, 12, 10, 0),
             status_str="Running 2 mins LATE", delay_seconds=120, eta_minutes=11.0,
             predicted_arrival=datetime(2026, 6, 16, 12, 11, 0), eta_source="hybrid")

_orig_arp = eta.active_route90_positions
_orig_cvp = eta.compute_vehicle_prediction
try:
    pos1 = Pos("V1", SANIDA, "T", 34.0, 33.0, PING_TS)
    pos4 = Pos("V4", SANIDA, "T", 34.0, 33.0, PING_TS)
    eta.active_route90_positions = lambda session, now=None: [pos1, pos4]
    eta.compute_vehicle_prediction = lambda session, pos, now=None: (
        p_at if pos.vehicle_id == "V1" else p_sched)
    text = predict_eta.get_prediction_text(now=NOW, session=object())
finally:
    eta.active_route90_positions = _orig_arp
    eta.compute_vehicle_prediction = _orig_cvp

expected_text = "\n".join(
    [
        "==================================================",
        "LIVE ETA PREDICTION: PYRGOS CHURCH",
        "==================================================",
        "",
    ]
    + predict_eta.format_prediction(p_at)
    + predict_eta.format_prediction(p_sched)
)
check("get_prediction_text assembles header + vehicles exactly",
      text == expected_text, "" if text == expected_text else f"got:\n{text}")


print("3. Logger (active_route90_positions + log_predictions field mapping/filtering)")


def fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_ping(vehicle_id, route_id, trip_id, ts):
    return VehiclePosition(vehicle_id=vehicle_id, trip_id=trip_id, route_id=route_id,
                           latitude=34.0, longitude=33.0, timestamp=ts, is_stationary=False)


session = fresh_session()
# BUS_FWD has two pings; the newer must be the one used (dedup test).
session.add_all([
    make_ping("BUS_FWD", SANIDA, "TF", PING_TS - timedelta(seconds=60)),
    make_ping("BUS_FWD", SANIDA, "TF", PING_TS),
    make_ping("BUS_PASS", SANIDA, "TP", PING_TS),
    make_ping("BUS_AT", SANIDA, "TA", PING_TS),
    make_ping("BUS_MOVE", SANIDA, "TM", PING_TS),
    make_ping("BUS_SKIP", SANIDA, "TS", PING_TS),
])
session.commit()

active = eta.active_route90_positions(session, now=NOW)
by_id = {p.vehicle_id: p for p in active}
check("active_route90_positions dedups to 5 vehicles", len(active) == 5, f"{len(active)}")
check("dedup keeps the NEWEST ping for BUS_FWD",
      by_id.get("BUS_FWD") is not None and by_id["BUS_FWD"].timestamp == PING_TS,
      f"{by_id.get('BUS_FWD') and by_id['BUS_FWD'].timestamp}")

canned = {
    "BUS_FWD": VP(vehicle_id="BUS_FWD", route_id=SANIDA, direction="Towards Sanida",
                  status="scheduled", predicted_at=NOW, trip_id="TF", stop_id="7604",
                  smooth_distance_m=4760.0, speed_kmh=28.8, moving=True, on_route=True,
                  has_schedule=True, delay_seconds=120, eta_minutes=11.0,
                  predicted_arrival=NOW + timedelta(minutes=11), eta_source="hybrid"),
    "BUS_MOVE": VP(vehicle_id="BUS_MOVE", route_id=SANIDA, direction="Towards Sanida",
                   status="no_schedule", predicted_at=NOW, trip_id="TM", stop_id="7604",
                   smooth_distance_m=3000.0, speed_kmh=21.6, moving=True, on_route=True,
                   eta_minutes=10.0, predicted_arrival=NOW + timedelta(minutes=10),
                   eta_source="move"),
    "BUS_PASS": VP(vehicle_id="BUS_PASS", route_id=SANIDA, direction="Towards Sanida",
                   status="passed", predicted_at=NOW, has_passed=True, eta_minutes=4.0,
                   eta_source="move"),  # has_passed -> skipped
    "BUS_AT": VP(vehicle_id="BUS_AT", route_id=SANIDA, direction="Towards Sanida",
                 status="at", predicted_at=NOW),  # no forward arrival -> skipped
    "BUS_SKIP": None,  # compute returned None -> skipped
}

_orig_cvp = eta.compute_vehicle_prediction
try:
    eta.compute_vehicle_prediction = lambda session, pos, now=None: canned.get(pos.vehicle_id)
    n = eta.log_predictions(session, now=NOW)
finally:
    eta.compute_vehicle_prediction = _orig_cvp

rows = session.query(PredictionLog).order_by(PredictionLog.vehicle_id).all()
logged_ids = [r.vehicle_id for r in rows]
check("log_predictions returns count of forward rows (2)", n == 2, f"n={n}")
check("only forward predictions logged (BUS_FWD, BUS_MOVE)",
      logged_ids == ["BUS_FWD", "BUS_MOVE"], f"{logged_ids}")
check("passed / at / None were skipped",
      not ({"BUS_PASS", "BUS_AT", "BUS_SKIP"} & set(logged_ids)), f"{logged_ids}")

by_row = {r.vehicle_id: r for r in rows}
fwd = by_row.get("BUS_FWD")
if fwd is not None:
    check("lead_time_min == (predicted_arrival - predicted_at)/60",
          approx(fwd.lead_time_min, (fwd.predicted_arrival - fwd.predicted_at).total_seconds() / 60)
          and approx(fwd.lead_time_min, 11.0), f"{fwd.lead_time_min}")
    check("field mapping (stop_id/eta_source/distance/delay/on_route)",
          fwd.stop_id == "7604" and fwd.eta_source == "hybrid"
          and approx(fwd.distance_m, 4760.0) and fwd.delay_seconds == 120
          and fwd.on_route is True, f"stop={fwd.stop_id} src={fwd.eta_source}")
move = by_row.get("BUS_MOVE")
if move is not None:
    check("no_schedule row: delay None, lead 10.0, eta_source 'move'",
          move.delay_seconds is None and approx(move.lead_time_min, 10.0)
          and move.eta_source == "move", f"delay={move.delay_seconds} lead={move.lead_time_min}")
session.close()


print("4. Migration on a COPY of the live DB")
if not HAVE_LIVE:
    check("live DB present", False, "data/bus_data.db not found - skipping migration check")
else:
    def snapshot(path):
        con = sqlite3.connect(path)
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'").fetchall()]
        counts = {t: con.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
        con.close()
        return counts

    before = snapshot(COPY_DB)
    engine = migrate_db()
    after1 = snapshot(COPY_DB)
    migrate_db()
    after2 = snapshot(COPY_DB)
    engine.dispose()

    con = sqlite3.connect(COPY_DB)
    cols = {c[1] for c in con.execute("PRAGMA table_info(prediction_logs)").fetchall()}
    idx = {i[0] for i in con.execute(
        "select name from sqlite_master where type='index' and tbl_name='prediction_logs'").fetchall()}
    con.close()

    expected_cols = {"id", "vehicle_id", "trip_id", "route_id", "stop_id", "predicted_at",
                     "predicted_arrival", "lead_time_min", "eta_source", "distance_m",
                     "speed_kmh", "delay_seconds", "is_stationary", "is_stale", "on_route",
                     "recorded_at"}
    check("prediction_logs created with all columns", expected_cols <= cols,
          f"missing {expected_cols - cols}")
    check("ix_predlog_trip_stop index created", "ix_predlog_trip_stop" in idx, f"{idx}")
    check("migration idempotent (counts stable across two runs)", after1 == after2,
          f"{after1} vs {after2}")
    # Pre-existing tables keep their row counts (prediction_logs is new at 0).
    preserved = all(before[t] == after1.get(t) for t in before)
    check("existing rows untouched", preserved and after1.get("prediction_logs") == 0,
          f"before={before} after={after1}")


print("5. Tier A regression guards (new robustness/correctness behavior)")

# --- #2: get_trip_state survives an empty trip<->stops merge -------------------
# A GTFS-integrity gap where none of the trip's stops resolve in stops.txt makes
# pd.merge empty; the old code then raised ValueError on idxmin(). The guard must
# return the schedule fallback instead. Fully isolated via monkeypatch so it needs
# no live CSV layout. Restored in finally (test-hygiene).
import pandas as _pd_real  # noqa: E402
_o_exists, _o_readcsv, _o_merge = eta.os.path.exists, eta.pd.read_csv, eta.pd.merge


def _fake_read_csv(path, **kw):
    if 'stop_times' in path:
        return _pd_real.DataFrame({'trip_id': ['T1', 'T1'],
                                   'arrival_time': ['12:05:00', '12:10:00'],
                                   'stop_id': ['9999', '7604'],
                                   'stop_sequence': ['1', '2']})
    return _pd_real.DataFrame({'stop_id': ['7604'], 'stop_lat': ['34.7'], 'stop_lon': ['33.1']})


_raised2 = False
try:
    eta.os.path.exists = lambda path: True
    eta.pd.read_csv = _fake_read_csv
    eta.pd.merge = lambda *a, **kw: _pd_real.DataFrame(
        columns=['trip_id', 'arrival_time', 'stop_id', 'stop_sequence', 'stop_lat', 'stop_lon'])
    res2 = eta.get_trip_state("T1", Pos("V2", SANIDA, "T1", 34.0, 33.0, PING_TS))
except Exception as e:  # the bug would surface here as ValueError
    _raised2, res2 = True, ("RAISED", repr(e))
finally:
    eta.os.path.exists, eta.pd.read_csv, eta.pd.merge = _o_exists, _o_readcsv, _o_merge
check("#2 empty merge -> schedule fallback, no ValueError",
      not _raised2 and res2 == (0, -1, 2, datetime(2026, 6, 16, 12, 10, 0)),
      f"raised={_raised2} res={res2}")

# --- #7: extrapolation no longer collapses a measured-far bus into a false 'at' -
# Bus 250 m out, 30 s ping, SMA 12 m/s -> raw movement 360 m once exceeded the
# 250 m gap and smooth->0 -> status 'at' (dropped from PredictionLog). Capped at
# 80% of 250 m = 200 m, smooth = 50 m, and raw 250 m > radius so it stays forward.
stub((0, -1, -1, None), 12.0, on_route(250, False))
try:
    p7 = compute()
finally:
    restore()
check("#7 fast bus 250m out stays a forward prediction (no premature 'at')",
      p7.status == "no_schedule" and p7.predicted_arrival is not None
      and approx(p7.smooth_distance_m, 50.0),
      f"status={p7.status} smooth={p7.smooth_distance_m} arr={p7.predicted_arrival}")

# --- #5: scheduled+moving hybrid falls back to movement ETA when non-positive --
# Very late (10 min) bus 15 km out: weight_move collapses to 0 so the hybrid would
# be profile_eta_min = -20 (projected arrival 20 min in the past). Instead of a
# negative ETA (pre-fix) or a fake-imminent 0, it falls back to the movement-based
# estimate: smooth = 15000 - min(8*30, 0.8*15000) = 14760; 14760 / 8 / 60 = 30.75 min.
stub((600, 5, 10, datetime(2026, 6, 16, 11, 30, 0)), 8.0, on_route(15000, False))
try:
    p5 = compute()
finally:
    restore()
check("#5 late+far moving bus -> movement-ETA fallback (no negative, no fake-0)",
      p5.status == "scheduled" and p5.eta_source == "move"
      and approx(p5.eta_minutes, 30.75)
      and p5.predicted_arrival == NOW + timedelta(minutes=30.75)
      and p5.status_str == "Running 10 mins LATE",
      f"eta={p5.eta_minutes} src={p5.eta_source} arr={p5.predicted_arrival} status_str={p5.status_str!r}")

# --- #3: get_prediction_text skips a raising vehicle, renders the rest ---------
_o_arp3, _o_cvp3 = eta.active_route90_positions, eta.compute_vehicle_prediction
_good3 = VP(vehicle_id="VOK", route_id=SANIDA, direction="Towards Sanida",
            status="at", predicted_at=NOW)


def _raise_for_bad(session, pos, now=None):
    if pos.vehicle_id == "VBAD":
        raise ValueError("simulated GTFS-integrity gap")
    return _good3


try:
    # VBAD first: proves the loop continues past a raise on the FIRST vehicle.
    eta.active_route90_positions = lambda session, now=None: [
        Pos("VBAD", SANIDA, "T", 34.0, 33.0, PING_TS),
        Pos("VOK", SANIDA, "T", 34.0, 33.0, PING_TS)]
    eta.compute_vehicle_prediction = _raise_for_bad
    text3 = predict_eta.get_prediction_text(now=NOW, session=object())
finally:
    eta.active_route90_positions, eta.compute_vehicle_prediction = _o_arp3, _o_cvp3
check("#3 bot reply survives a raising vehicle and still renders the healthy one",
      "VOK" in text3 and "AT PYRGOS CHURCH" in text3 and "VBAD" not in text3,
      f"text={text3!r}")

# --- #4: log_predictions isolates a raising vehicle; healthy rows still persist -
session4 = fresh_session()
session4.add_all([
    make_ping("GOOD1", SANIDA, "G1", PING_TS),
    make_ping("BADV", SANIDA, "BV", PING_TS),
    make_ping("GOOD2", SANIDA, "G2", PING_TS),
])
session4.commit()
_canned4 = {
    "GOOD1": VP(vehicle_id="GOOD1", route_id=SANIDA, direction="Towards Sanida",
                status="no_schedule", predicted_at=NOW, trip_id="G1", stop_id="7604",
                smooth_distance_m=3000.0, speed_kmh=21.6, moving=True, on_route=True,
                eta_minutes=10.0, predicted_arrival=NOW + timedelta(minutes=10), eta_source="move"),
    "GOOD2": VP(vehicle_id="GOOD2", route_id=SANIDA, direction="Towards Sanida",
                status="no_schedule", predicted_at=NOW, trip_id="G2", stop_id="7604",
                smooth_distance_m=2000.0, speed_kmh=18.0, moving=True, on_route=True,
                eta_minutes=8.0, predicted_arrival=NOW + timedelta(minutes=8), eta_source="move"),
}


def _raise_mid(session, pos, now=None):
    if pos.vehicle_id == "BADV":
        raise ValueError("simulated compute failure")
    return _canned4.get(pos.vehicle_id)


_o_cvp4 = eta.compute_vehicle_prediction
try:
    eta.compute_vehicle_prediction = _raise_mid
    n4 = eta.log_predictions(session4, now=NOW)
finally:
    eta.compute_vehicle_prediction = _o_cvp4
rows4 = {r.vehicle_id for r in session4.query(PredictionLog).all()}
check("#4 raising vehicle isolated: healthy rows still committed (count 2)",
      n4 == 2 and rows4 == {"GOOD1", "GOOD2"}, f"n={n4} rows={rows4}")
session4.close()

# --- #4b: a DBAPIError that POISONS the session mid-loop still doesn't lose the
# healthy rows. commit-per-vehicle + rollback recovers; the earlier batch-commit
# version would have raised PendingRollbackError at the final commit and lost all. -
from sqlalchemy import text as _sa_text  # noqa: E402
session4b = fresh_session()
session4b.add_all([
    make_ping("OK1", SANIDA, "K1", PING_TS),
    make_ping("POISON", SANIDA, "PZ", PING_TS),
    make_ping("OK2", SANIDA, "K2", PING_TS),
])
session4b.commit()
_canned4b = {
    "OK1": VP(vehicle_id="OK1", route_id=SANIDA, direction="Towards Sanida", status="no_schedule",
              predicted_at=NOW, trip_id="K1", stop_id="7604", smooth_distance_m=1000.0, speed_kmh=18.0,
              moving=True, on_route=True, eta_minutes=5.0, predicted_arrival=NOW + timedelta(minutes=5),
              eta_source="move"),
    "OK2": VP(vehicle_id="OK2", route_id=SANIDA, direction="Towards Sanida", status="no_schedule",
              predicted_at=NOW, trip_id="K2", stop_id="7604", smooth_distance_m=1500.0, speed_kmh=20.0,
              moving=True, on_route=True, eta_minutes=6.0, predicted_arrival=NOW + timedelta(minutes=6),
              eta_source="move"),
}


def _poison_then_raise(session, pos, now=None):
    if pos.vehicle_id == "POISON":
        try:
            session.execute(_sa_text("SELECT 1 FROM _no_such_table_xyz"))  # marks tx rollback-only
        except Exception:
            pass
        raise RuntimeError("compute failed after a DB error poisoned the session")
    return _canned4b.get(pos.vehicle_id)


_o_cvp4b = eta.compute_vehicle_prediction
try:
    eta.compute_vehicle_prediction = _poison_then_raise
    n4b = eta.log_predictions(session4b, now=NOW)
finally:
    eta.compute_vehicle_prediction = _o_cvp4b
rows4b = {r.vehicle_id for r in session4b.query(PredictionLog).all()}
check("#4b session-poisoning DBAPIError isolated: healthy rows survive",
      n4b == 2 and rows4b == {"OK1", "OK2"}, f"n={n4b} rows={rows4b}")
session4b.close()

# --- #2b: the empty-merge fallback (current_seq=-1, positive target_seq) flows
# correctly through compute -> 'scheduled', never a false 'passed' via the
# off-route stop-sequence fallback (target_seq != -1 and current_seq > target_seq). -
stub((0, -1, 5, datetime(2026, 6, 16, 12, 10, 0)), 4.0, {"ok": False})
try:
    p2b = compute()
finally:
    restore()
check("#2b fallback (current_seq=-1) -> scheduled branch, not false 'passed'",
      p2b is not None and p2b.status == "scheduled" and not p2b.has_passed,
      f"status={getattr(p2b, 'status', None)} has_passed={getattr(p2b, 'has_passed', None)}")


print("6. Tier B regression guards (clock-skew / fallback-stop hardening)")

# --- #6: off-route straight-line fallback measures to the DIRECTION'S target stop,
# not one hard-coded church coordinate. Stops 7604 (Sanida) and 5411 (Lemesos) are
# ~57 m apart, so the Lemesos fallback must measure to 5411, not STOP_LAT/LON (==7604).
# stop_coordinates is NOT stubbed -> needs the real stops.txt; skip cleanly if absent.
_b6_5411 = eta.mm.stop_coordinates("5411")
_b6_7604 = eta.mm.stop_coordinates("7604")
if _b6_5411 is None or _b6_7604 is None:
    check("#6 stops.txt present for fallback-coords check", False,
          "stop coords unavailable - skipping #6")
else:
    _B6_LAT, _B6_LON = 34.75, 33.19  # ~1.1 km NE of the church, off-route
    stub((0, -1, 5, datetime(2026, 6, 16, 12, 10, 0)), 0.0, {"ok": False})  # off-route, speed 0
    try:
        p6_lem = compute(route_id=LEMESOS, lat=_B6_LAT, lon=_B6_LON)
        p6_san = compute(route_id=SANIDA, lat=_B6_LAT, lon=_B6_LON)
    finally:
        restore()
    exp_lem = eta.haversine(_b6_5411[0], _b6_5411[1], _B6_LAT, _B6_LON)
    exp_san = eta.haversine(_b6_7604[0], _b6_7604[1], _B6_LAT, _B6_LON)
    exp_bug = eta.haversine(eta.STOP_LAT, eta.STOP_LON, _B6_LAT, _B6_LON)  # old behaviour (7604)
    check("#6 Lemesos off-route fallback measures to stop 5411, not hard-coded 7604",
          approx(p6_lem.smooth_distance_m, exp_lem, tol=0.5)
          and not approx(p6_lem.smooth_distance_m, exp_bug, tol=5.0),
          f"smooth={p6_lem.smooth_distance_m:.1f} 5411={exp_lem:.1f} 7604/bug={exp_bug:.1f}")
    check("#6 Sanida off-route fallback still measures to stop 7604",
          approx(p6_san.smooth_distance_m, exp_san, tol=0.5),
          f"smooth={p6_san.smooth_distance_m:.1f} 7604={exp_san:.1f}")

# --- #8: a single future-dated ping (one vehicle's clock skew) must NOT push the
# 15-min activity window past genuinely-live buses. With effective_now clamped to
# now+2min, a real bus pinging at ~now stays visible. (Pre-fix: effective_now =
# now+20min -> threshold now+5min -> the live bus at now-30s is excluded.)
session8 = fresh_session()
session8.add_all([
    make_ping("LIVE", SANIDA, "L1", NOW - timedelta(seconds=30)),   # genuinely live
    make_ping("SKEW", SANIDA, "S1", NOW + timedelta(minutes=20)),   # clock-skewed future ping
])
session8.commit()
active8 = {p.vehicle_id for p in eta.active_route90_positions(session8, now=NOW)}
check("#8 future-skewed ping no longer hides the live bus (clamp to now+2min)",
      active8 == {"LIVE", "SKEW"}, f"{active8}")
session8.close()

# #8b: the clamp only WIDENS the window vs the old anchor, so guard the other side --
# in the normal case (no future-skewed ping) the 15-min activity window must still
# EXCLUDE a genuinely stale bus pinged 20 min ago. Proves the fix didn't over-loosen.
session8b = fresh_session()
session8b.add_all([
    make_ping("LIVE", SANIDA, "L2", NOW - timedelta(seconds=30)),   # newest ping ~ now
    make_ping("STALE", SANIDA, "ST", NOW - timedelta(minutes=20)),  # 20 min old -> out of window
])
session8b.commit()
active8b = {p.vehicle_id for p in eta.active_route90_positions(session8b, now=NOW)}
check("#8b normal 15-min window still excludes a 20-min-old ping (clamp didn't over-widen)",
      active8b == {"LIVE"}, f"{active8b}")
session8b.close()

# --- #1: get_engine() is now a process-wide singleton, and its connect listener
# applies busy_timeout + WAL to EVERY connection (the runtime path), not only
# migrate_db. Runs against the throwaway COPY_DB (DATABASE_URL was redirected at
# import). Dispose after so the final cleanup can unlink the file on Windows.
from db.models import get_engine as _get_engine  # noqa: E402
_e1 = _get_engine()
_e2 = _get_engine()
check("#1 get_engine() returns one shared Engine instance (no per-tick rebuild)",
      _e1 is _e2, f"same={_e1 is _e2}")
with _e1.connect() as _conn:
    _bt = _conn.execute(_sa_text("PRAGMA busy_timeout")).scalar()
    _jm = _conn.execute(_sa_text("PRAGMA journal_mode")).scalar()
check("#1 runtime connection carries busy_timeout=30000 and journal_mode=WAL",
      _bt == 30000 and str(_jm).lower() == "wal", f"busy_timeout={_bt} journal_mode={_jm}")
_e1.dispose()


# Cleanup the throwaway copy + its WAL/SHM sidecars. The engine was disposed above;
# force a GC so any lingering SQLite handle is released before we unlink (Windows
# locks open files).
import gc  # noqa: E402
gc.collect()
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(COPY_DB + _suffix)
    except OSError as e:
        if _suffix == "":
            print(f"  (note: could not remove {COPY_DB} - gitignored, overwritten next run: {e})")

print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
