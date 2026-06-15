"""
Verification for multi-stop arrival capture (Phase 3, step 0) in
analysis/geofence.py._log_crossings.

Run directly:  python tests/test_multi_stop_capture.py
Exit code 0 = all checks passed, 1 = a check failed.

Checks:
  1. Synthetic ping-pair that brackets exactly 3 stops -> 3 events, correct stop
     order, strictly increasing interpolated arrival times.
  2. Boundary semantics: the lower-bracket stop (s == a1) is excluded, the
     upper-bracket stop (s == a0) is included.
  3. Re-running the same pair logs nothing new (dedup via the logged-set and the
     UNIQUE(trip_id, stop_id) index).
  4. Backfill replay: feed a real stored trip's pings through _log_crossings pair
     by pair (as the monitor would) -> many stops captured once, no duplicates,
     arrivals monotonic in stop_sequence.

Uses real GTFS (data/raw/static/Limassol) for geometry but an in-memory SQLite
DB for writes, so it never touches the live bus_data.db's stop_events.
"""

import os
import sys
import sqlite3
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, StopEvent, VehiclePosition  # noqa: E402
from analysis import map_matching as mm  # noqa: E402
from analysis import geofence  # noqa: E402

ROUTES = ["10900011", "10900012"]

_failures = []


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def fresh_session():
    """In-memory DB with the full schema (incl. the unique index from __table_args__)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_ping(route_id, trip_id, lat, lon, ts, vehicle_id="TESTV"):
    return VehiclePosition(
        vehicle_id=vehicle_id, trip_id=trip_id, route_id=route_id,
        latitude=lat, longitude=lon, timestamp=ts, is_stationary=False,
    )


print("1-3. Synthetic 3-stop bracket, boundary semantics, dedup")
route_id = ROUTES[0]
stops = sorted(mm.ordered_stops_for_route(route_id), key=lambda s: s["dist_along_m"])
if len(stops) < 10:
    check("enough stops to build a synthetic bracket", False, f"only {len(stops)} stops")
else:
    j = 5  # bracket stops j+1, j+2, j+3 by placing pings at stops j and j+3
    lower, upper = stops[j], stops[j + 3]
    expected = [stops[j + 1]["stop_id"], stops[j + 2]["stop_id"], stops[j + 3]["stop_id"]]

    base = datetime(2026, 6, 16, 12, 0, 0)
    pos1 = make_ping(route_id, "TEST_TRIP_1", lower["lat"], lower["lon"], base)
    pos0 = make_ping(route_id, "TEST_TRIP_1", upper["lat"], upper["lon"], base + timedelta(seconds=60))

    session = fresh_session()
    n = geofence._log_crossings(session, pos0, pos1, quiet=True)
    check("brackets exactly 3 stops", n == 3, f"logged {n}")

    rows = session.query(StopEvent).order_by(StopEvent.actual_arrival_time).all()
    got = [r.stop_id for r in rows]
    check("captured stop order matches along-route order", got == expected,
          f"got {got} expected {expected}")

    arrivals = [r.actual_arrival_time for r in rows]
    strictly_increasing = all(arrivals[i] < arrivals[i + 1] for i in range(len(arrivals) - 1))
    check("interpolated arrivals strictly increasing", strictly_increasing,
          f"{[a.strftime('%H:%M:%S') for a in arrivals]}")

    check("lower-bracket stop (s==a1) excluded", lower["stop_id"] not in got)
    check("upper-bracket stop (s==a0) included", upper["stop_id"] in got)

    seqs = [r.stop_sequence for r in rows]
    check("stop_sequence recorded", all(s is not None for s in seqs), f"{seqs}")
    methods = {r.method for r in rows}
    check("method tagged 'along_route'", methods == {"along_route"}, f"{methods}")

    # Re-run the identical pair: nothing new (logged-set + unique index).
    n2 = geofence._log_crossings(session, pos0, pos1, quiet=True)
    total = session.query(StopEvent).count()
    check("re-running the same pair adds nothing (dedup)", n2 == 0 and total == 3,
          f"second pass logged {n2}, total {total}")
    session.close()

print("4. Backfill replay of a real stored trip")
db = os.path.join(ROOT, "data", "bus_data.db")
if not os.path.exists(db):
    check("DB present", False, "data/bus_data.db not found - skipping backfill check")
else:
    con = sqlite3.connect(db)
    row = con.execute(
        "select trip_id, route_id, count(*) c from vehicle_positions "
        "where route_id in ('10900011','10900012') and trip_id is not null "
        "group by trip_id order by c desc limit 1"
    ).fetchone()
    if not row:
        check("route-90 pings present", False, "no route-90 pings in DB")
    else:
        trip_id, rid, cnt = row
        pings = con.execute(
            "select vehicle_id, latitude, longitude, timestamp, is_stationary "
            "from vehicle_positions where trip_id=? order by timestamp asc",
            (trip_id,),
        ).fetchall()
        route_stops = mm.ordered_stops_for_route(rid)
        n_route_stops = len(route_stops)

        session = fresh_session()
        prev = None
        for vid, lat, lon, ts, stat in pings:
            # SQLite stores timestamps as ISO strings; parse back to datetime.
            tsd = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
            cur = make_ping(rid, trip_id, lat, lon, tsd, vehicle_id=str(vid))
            if prev is not None:
                geofence._log_crossings(session, cur, prev, quiet=True)
            prev = cur

        rows = session.query(StopEvent).order_by(StopEvent.stop_sequence).all()
        captured = len(rows)

        # No duplicates (the unique index would also have blocked them).
        pairs = [(r.trip_id, r.stop_id) for r in rows]
        no_dupes = len(pairs) == len(set(pairs))

        # Monotonic arrivals in stop_sequence order.
        arrivals = [r.actual_arrival_time for r in rows]
        monotonic = all(arrivals[i] <= arrivals[i + 1] for i in range(len(arrivals) - 1))

        print(f"     trip {trip_id} on route {rid}: {cnt} pings, "
              f"captured {captured}/{n_route_stops} route stops")
        check("backfill captures many stops (>> the old single-stop)",
              captured >= 10, f"captured {captured}")
        check("no duplicate (trip_id, stop_id) rows", no_dupes,
              f"{len(pairs)} rows, {len(set(pairs))} unique")
        check("arrivals monotonic in stop_sequence", monotonic,
              "non-monotonic arrival detected" if not monotonic else "")
        session.close()
    con.close()

print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
