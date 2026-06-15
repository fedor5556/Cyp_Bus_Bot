"""
Verification for analysis/map_matching.py.

Run directly:  python tests/test_map_matching.py
Exit code 0 = all checks passed, 1 = a check failed.

Checks:
  1. Shape points project to ~0 cross-track at their own cumulative distance.
  2. route_id -> shape resolves for both Route 90 directions.
  3. Stops in schedule order come out in increasing along-route order (the core
     map-matching correctness property; catches polyline mis-snaps).
  4. Target stops 7604 / 5411 snap tightly to their direction's shape.
  5. Off-route points are flagged (large cross-track, on_route False).
  6. Real DB pings: on-route cross-track is small and a trip progresses
     monotonically along the route; report straight-line vs along-route bias.
"""

import os
import sys
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from analysis import map_matching as mm  # noqa: E402

ROUTES = ["10900011", "10900012"]
TARGET = {"10900011": "5411", "10900012": "7604"}

_failures = []


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


print("1. Shape-point self-projection (cross-track ~0, along == cumulative)")
for rid in ROUTES:
    sid = mm.shape_for_route(rid)
    mm._ensure_loaded()
    shp = mm._cache["shapes"][sid]
    # sample a few interior points
    idxs = [len(shp["lat"]) // 4, len(shp["lat"]) // 2, 3 * len(shp["lat"]) // 4]
    worst_cross = 0.0
    worst_along_err = 0.0
    for i in idxs:
        along, cross = mm.project_point(sid, shp["lat"][i], shp["lon"][i])
        worst_cross = max(worst_cross, cross)
        worst_along_err = max(worst_along_err, abs(along - shp["cum"][i]))
    check(f"route {rid}: shape points snap to ~0",
          worst_cross < 1.0 and worst_along_err < 5.0,
          f"max cross={worst_cross:.2f}m, max along-err={worst_along_err:.2f}m")

print("2. route_id -> shape resolves")
for rid in ROUTES:
    sid = mm.shape_for_route(rid)
    check(f"route {rid} has a shape", sid is not None, f"shape={sid}")

print("3. Stops in schedule order are in increasing along-route order")
for rid in ROUTES:
    stops = mm.ordered_stops_for_route(rid)
    check(f"route {rid}: ordered_stops non-empty", len(stops) > 5, f"{len(stops)} stops")
    if not stops:
        continue
    along = [s["dist_along_m"] for s in stops]
    # count inversions (a later-sequence stop with smaller along-distance)
    inversions = sum(1 for i in range(len(along) - 1) if along[i + 1] < along[i] - 5.0)
    max_cross = max(s["cross_track_m"] for s in stops)
    check(f"route {rid}: monotonic along-route stop order",
          inversions == 0,
          f"{inversions} inversions over {len(stops)} stops; max stop cross-track={max_cross:.1f}m")

print("4. Target stops snap tightly to their direction's shape")
for rid in ROUTES:
    sid = mm.shape_for_route(rid)
    stop = TARGET[rid]
    res = mm.distance_along_for_stop(sid, stop)
    check(f"route {rid}: stop {stop} snaps tight",
          res is not None and res[1] < 50.0,
          f"along={res[0]:.0f}m, cross={res[1]:.1f}m" if res else "no projection")

print("5. Off-route point is flagged")
# A point well away from Limassol (Nicosia-ish) must be off-route.
res = mm.route_distance_to_stop("10900012", 35.17, 33.36, "7604")
check("off-route point flagged", res["ok"] and not res["on_route"],
      f"cross={res.get('cross_track_m', float('nan')):.0f}m, on_route={res.get('on_route')}")

print("6. Real DB pings: cross-track + trip monotonicity + straight-line bias")
db = os.path.join(ROOT, "data", "bus_data.db")
if not os.path.exists(db):
    check("DB present", False, "data/bus_data.db not found - skipping live checks")
else:
    con = sqlite3.connect(db)
    # pick the route-90 trip with the most pings for a monotonicity check
    row = con.execute(
        "select trip_id, route_id, count(*) c from vehicle_positions "
        "where route_id in ('10900011','10900012') and trip_id is not null "
        "group by trip_id order by c desc limit 1"
    ).fetchone()
    if not row:
        check("route-90 pings present", False, "no route-90 pings in DB")
    else:
        trip_id, route_id, cnt = row
        sid = mm.shape_for_route(route_id)
        pings = con.execute(
            "select latitude, longitude from vehicle_positions "
            "where trip_id=? order by timestamp asc", (trip_id,)
        ).fetchall()
        alongs, crosses = [], []
        for lat, lon in pings:
            a, c = mm.project_point(sid, lat, lon)
            alongs.append(a)
            crosses.append(c)
        import statistics
        med_cross = statistics.median(crosses)
        # progression: net along-route travel should dominate backward jitter
        steps = [alongs[i + 1] - alongs[i] for i in range(len(alongs) - 1)]
        fwd = sum(s for s in steps if s > 0)
        bwd = -sum(s for s in steps if s < 0)
        check(f"trip {trip_id}: on-route median cross-track small",
              med_cross < 60.0, f"median cross={med_cross:.1f}m over {cnt} pings")
        check(f"trip {trip_id}: forward travel dominates",
              fwd > bwd * 3, f"forward={fwd:.0f}m backward={bwd:.0f}m")

        # straight-line vs along-route distance to the direction's target stop
        target = TARGET[route_id]
        tcoords = mm._cache["stop_coords"][target]
        diffs = []
        for lat, lon in pings:
            sl = mm.haversine(lat, lon, tcoords[0], tcoords[1])
            rd = mm.route_distance_to_stop(route_id, lat, lon, target)
            if rd["ok"] and rd["on_route"]:
                diffs.append(rd["abs_distance_m"] - sl)
        if diffs:
            import statistics as st
            print(f"     straight-line vs along-route to stop {target}: "
                  f"median gap={st.median(diffs):.0f}m, max={max(diffs):.0f}m "
                  f"(road distance is longer, as expected)")
            check("along-route >= straight-line on average", st.median(diffs) >= -5.0,
                  f"median gap {st.median(diffs):.0f}m")
    con.close()

print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
