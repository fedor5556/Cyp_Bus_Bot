"""
map_matching.py - Route polyline snapping (Phase 3, step 3).

Projects GPS pings and stops onto a route's shapes.txt polyline so that distance
is measured ALONG THE ROAD instead of as a straight ("crow-flies") line.

Why this exists:
  * predict_eta.py measured distance-to-stop with a straight-line haversine, so a
    bus 6 km away by road but 3 km in a straight line was treated as 3 km away.
    That bias is always optimistic (says the bus is closer than it is) and is the
    main source of ETA error.
  * It is also the geometric foundation for clean multi-stop arrival capture: at
    30-40 s ping gaps a single ping-pair can straddle several stops, and only
    along-route distance can order/interpolate those crossings without producing
    physically impossible (non-monotonic) arrival times.

Design notes:
  * Route 90 has exactly one shape per direction (10900011 -> shape 10900011,
    10900012 -> shape 10900012), keyed off the STABLE route_id, not the rotating
    trip_id. So the live path never depends on a trip_id that the 12 h GTFS update
    will invalidate.
  * shape_dist_traveled is absent from this feed, so cumulative distance is
    computed here (haversine between consecutive shape points).
  * All loaders are cached and invalidated on file mtime change, so the 12 h
    schedule auto-update is picked up automatically.
  * Pure stdlib + pandas. No new dependencies. Every public function fails soft
    (returns None / ok=False) so a missing or malformed shape can never crash a
    caller - the caller falls back to the old haversine path.
"""

import os
import sys
import math

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

import pandas as pd

# A ping whose nearest point on the polyline is farther than this is treated as
# "off route" (depot, GPS glitch, a bus not actually on Route 90's geometry).
# Route 90 buses that are genuinely on the road snap to within ~10-40 m.
OFF_ROUTE_THRESHOLD_M = 150.0

_CITY = "Limassol"

# ---- module-level cache (mtime-invalidated) --------------------------------
_cache = {
    "mtimes": None,                 # tuple of source-file mtimes when last built
    "shapes": {},                   # shape_id -> {"lat","lon","cum","seglen","length"}
    "route_shape": {},              # route_id -> shape_id
    "stop_coords": {},              # stop_id -> (lat, lon)
    "stop_along": {},               # (shape_id, stop_id) -> (dist_along, cross_track)
    "ordered_stops": {},            # route_id -> ordered_stops_for_route(route_id) result
}


def _paths():
    base = os.path.join(Config.STATIC_DATA_DIR, _CITY)
    return {
        "shapes": os.path.join(base, "shapes.txt"),
        "trips": os.path.join(base, "trips.txt"),
        "stops": os.path.join(base, "stops.txt"),
    }


def _mtimes(paths):
    out = []
    for p in paths.values():
        try:
            out.append(os.path.getmtime(p))
        except OSError:
            out.append(None)
    return tuple(out)


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters (kept local so this module is standalone)."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _ensure_loaded():
    """Rebuild the shape/route/stop caches if any source file changed on disk."""
    paths = _paths()
    mt = _mtimes(paths)
    if _cache["mtimes"] == mt and _cache["shapes"]:
        return _cache["shapes"]  # warm and current

    shapes = {}
    route_shape = {}
    stop_coords = {}

    # --- shapes: ordered points + cumulative along-route distance -------------
    if os.path.exists(paths["shapes"]):
        sdf = pd.read_csv(
            paths["shapes"],
            usecols=["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
            dtype={"shape_id": str},
        )
        sdf["shape_pt_sequence"] = sdf["shape_pt_sequence"].astype(int)
        sdf["shape_pt_lat"] = sdf["shape_pt_lat"].astype(float)
        sdf["shape_pt_lon"] = sdf["shape_pt_lon"].astype(float)
        for shape_id, grp in sdf.groupby("shape_id"):
            grp = grp.sort_values("shape_pt_sequence")
            lat = grp["shape_pt_lat"].tolist()
            lon = grp["shape_pt_lon"].tolist()
            if len(lat) < 2:
                continue
            cum = [0.0]
            seglen = []
            for i in range(len(lat) - 1):
                d = haversine(lat[i], lon[i], lat[i + 1], lon[i + 1])
                seglen.append(d)
                cum.append(cum[-1] + d)
            shapes[shape_id] = {
                "lat": lat,
                "lon": lon,
                "cum": cum,
                "seglen": seglen,
                "length": cum[-1],
            }

    # --- route_id -> shape_id (one shape per route in this feed) --------------
    if os.path.exists(paths["trips"]):
        tdf = pd.read_csv(paths["trips"], usecols=["route_id", "shape_id"], dtype=str)
        tdf = tdf.dropna(subset=["route_id", "shape_id"])
        for route_id, grp in tdf.groupby("route_id"):
            # Most common shape for the route (robust if rare variants ever appear).
            route_shape[route_id] = grp["shape_id"].mode().iloc[0]

    # --- stop coordinates -----------------------------------------------------
    if os.path.exists(paths["stops"]):
        stdf = pd.read_csv(paths["stops"], usecols=["stop_id", "stop_lat", "stop_lon"], dtype={"stop_id": str})
        for r in stdf.itertuples(index=False):
            try:
                stop_coords[str(r.stop_id)] = (float(r.stop_lat), float(r.stop_lon))
            except (TypeError, ValueError):
                continue

    _cache.update(
        mtimes=mt,
        shapes=shapes,
        route_shape=route_shape,
        stop_coords=stop_coords,
        stop_along={},      # invalidate derived projections
        ordered_stops={},   # invalidate derived ordered-stop lists
    )
    return shapes


def shape_for_route(route_id):
    """Return the shape_id used by a route, or None."""
    _ensure_loaded()
    return _cache["route_shape"].get(str(route_id))


def stop_coordinates(stop_id):
    """(lat, lon) for a stop_id straight from stops.txt, or None if unknown.

    Public accessor over the cached stop_coords table (mtime-invalidated via
    _ensure_loaded) so callers don't reach into the private _cache. Used by the
    straight-line ETA fallback to measure to the *correct* target stop per
    direction instead of one hard-coded church-side coordinate.
    """
    _ensure_loaded()
    return _cache["stop_coords"].get(str(stop_id))


def _to_local(lat, lon, lat0, lon0):
    """Equirectangular meters of (lat,lon) relative to (lat0,lon0)."""
    mlat = 110574.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    return ((lon - lon0) * mlon, (lat - lat0) * mlat)


def project_point(shape_id, lat, lon):
    """Snap a point to a shape's polyline.

    Returns (dist_along_m, cross_track_m) where dist_along_m is the distance from
    the shape's start to the snapped foot of the perpendicular, and cross_track_m
    is how far the point lies off the polyline. Returns None if the shape is
    unknown. Projection is done per-segment in a local tangent plane (accurate
    over the ~50 m spacing of these shapes); along-distance is anchored to the
    haversine cumulative table so it stays on a consistent metric scale.
    """
    # Fail soft on missing / non-numeric / non-finite coords (NaN, inf, None,
    # strings) so the "never raises" contract holds even if the feed schema drifts.
    try:
        if not (math.isfinite(lat) and math.isfinite(lon)):
            return None
    except TypeError:
        return None
    _ensure_loaded()
    shp = _cache["shapes"].get(str(shape_id))
    if not shp:
        return None
    lat_a, lon_a, cum, seglen = shp["lat"], shp["lon"], shp["cum"], shp["seglen"]

    best_cross = None
    best_along = 0.0
    for i in range(len(seglen)):
        ax, ay = 0.0, 0.0  # segment start is the local origin
        bx, by = _to_local(lat_a[i + 1], lon_a[i + 1], lat_a[i], lon_a[i])
        px, py = _to_local(lat, lon, lat_a[i], lon_a[i])
        abx, aby = bx - ax, by - ay
        denom = abx * abx + aby * aby
        if denom == 0.0:
            t = 0.0
        else:
            t = (px * abx + py * aby) / denom
            t = max(0.0, min(1.0, t))
        projx, projy = ax + t * abx, ay + t * aby
        cross = math.hypot(px - projx, py - projy)
        if best_cross is None or cross < best_cross:
            best_cross = cross
            best_along = cum[i] + t * seglen[i]

    return (best_along, best_cross)


def distance_along_for_stop(shape_id, stop_id):
    """Distance along `shape_id` at which `stop_id` sits (cached). Returns
    (dist_along_m, cross_track_m) or None if the stop or shape is unknown."""
    _ensure_loaded()
    key = (str(shape_id), str(stop_id))
    if key in _cache["stop_along"]:
        return _cache["stop_along"][key]
    coords = _cache["stop_coords"].get(str(stop_id))
    if not coords:
        return None
    res = project_point(shape_id, coords[0], coords[1])
    _cache["stop_along"][key] = res
    return res


def route_distance_to_stop(route_id, lat, lon, stop_id):
    """Along-route distance from a bus at (lat,lon) on `route_id` to `stop_id`.

    Returns a dict. On success: ok=True with
      route_distance_m  - signed: >0 the stop is ahead, <0 already passed
      abs_distance_m     - magnitude (what an ETA wants)
      on_route           - cross_track within OFF_ROUTE_THRESHOLD_M
      cross_track_m      - bus distance off the polyline
      passed             - route_distance_m < 0
      bus_along_m / stop_along_m / stop_cross_m
    On failure: ok=False with a `reason`. Callers should fall back to the
    straight-line haversine when ok is False or on_route is False.
    """
    shape_id = shape_for_route(route_id)
    if not shape_id:
        return {"ok": False, "reason": "no_shape_for_route"}
    bus = project_point(shape_id, lat, lon)
    stop = distance_along_for_stop(shape_id, stop_id)
    if bus is None or stop is None:
        return {"ok": False, "reason": "projection_failed", "shape_id": shape_id}
    bus_along, bus_cross = bus
    stop_along, stop_cross = stop
    route_distance = stop_along - bus_along
    return {
        "ok": True,
        "shape_id": shape_id,
        "route_distance_m": route_distance,
        "abs_distance_m": abs(route_distance),
        "on_route": bus_cross <= OFF_ROUTE_THRESHOLD_M,
        "cross_track_m": bus_cross,
        "passed": route_distance < 0,
        "bus_along_m": bus_along,
        "stop_along_m": stop_along,
        "stop_cross_m": stop_cross,
    }


def ordered_stops_for_route(route_id):
    """Ordered stop list for a route's direction, each snapped to the shape.

    Returns a list of dicts {stop_id, stop_sequence, lat, lon, dist_along_m,
    cross_track_m} sorted by stop_sequence, or [] on failure. Reads the stop
    sequence from a representative CURRENT trip of the route (trip_ids rotate, so
    we pick one from the live feed). Used by tests and by multi-stop arrival
    capture (geofence.py), which calls it on every monitor cycle - so the result
    is cached (mtime-invalidated via _ensure_loaded) to avoid re-reading
    trips.txt + the 6.4 MB stop_times.txt every 10 s.
    """
    _ensure_loaded()
    cached = _cache["ordered_stops"].get(str(route_id))
    if cached is not None:
        return cached

    def _compute():
        shape_id = shape_for_route(route_id)
        if not shape_id:
            return []
        base = os.path.join(Config.STATIC_DATA_DIR, _CITY)
        trips_path = os.path.join(base, "trips.txt")
        st_path = os.path.join(base, "stop_times.txt")
        if not (os.path.exists(trips_path) and os.path.exists(st_path)):
            return []
        tdf = pd.read_csv(trips_path, usecols=["route_id", "trip_id"], dtype=str)
        rtrips = tdf[tdf["route_id"] == str(route_id)]["trip_id"].tolist()
        if not rtrips:
            return []
        rep_trip = rtrips[0]
        stdf = pd.read_csv(st_path, usecols=["trip_id", "stop_id", "stop_sequence"], dtype=str)
        trip_stops = stdf[stdf["trip_id"] == rep_trip].copy()
        if trip_stops.empty:
            return []
        trip_stops["stop_sequence"] = trip_stops["stop_sequence"].astype(int)
        trip_stops = trip_stops.sort_values("stop_sequence")
        out = []
        for r in trip_stops.itertuples(index=False):
            coords = _cache["stop_coords"].get(str(r.stop_id))
            if not coords:
                continue
            proj = distance_along_for_stop(shape_id, str(r.stop_id))
            if proj is None:
                continue
            out.append({
                "stop_id": str(r.stop_id),
                "stop_sequence": int(r.stop_sequence),
                "lat": coords[0],
                "lon": coords[1],
                "dist_along_m": proj[0],
                "cross_track_m": proj[1],
            })
        return out

    result = _compute()
    _cache["ordered_stops"][str(route_id)] = result
    return result


if __name__ == "__main__":
    # Tiny smoke test; the real verification lives in tests/test_map_matching.py
    for rid in ("10900011", "10900012"):
        sid = shape_for_route(rid)
        shp = _cache["shapes"].get(sid)
        print(f"route {rid} -> shape {sid}, length {shp['length']/1000:.2f} km, pts {len(shp['lat'])}")
