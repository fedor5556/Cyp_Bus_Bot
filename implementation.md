# Phase 3 Implementation Plan

_Last updated: 2026-06-14_

## Guiding principle

Build in **data-readiness order, not the document's numbering.** The bottleneck for the
machine-learning payoff is not model code — it's the volume of *labelled* arrival data
(`stop_events`), which today sits in the low hundreds, far below ML-usable. So: deploy the
data taps first, let them fill, and advance each data-hungry step only when its data exists.
Nothing in the original Phase 3 plan is dropped — only re-ordered.

Two facts shape every step below (verified 2026-06-14 — re-check before relying on them):

1. **GTFS `trip_id`s rotate on every download.** 0 of the stored `stop_event` trip_ids existed
   in the then-current `trips.txt`; `calendar_dates.txt` maps each `service_id` to exactly one
   date. ⇒ `(trip_id, stop_id)` is globally unique (no `service_date` needed for dedup), and
   **detection must never be gated on a per-`trip_id` cache** (a bus mid-route across the 12 h
   GTFS swap would silently vanish). Key detection off `route_id` (stable).
2. **`init_db()` / `create_all` is never called on any runtime entrypoint** (only in
   `models.py`'s `__main__`), and `create_all` is additive-only. ⇒ any schema change needs an
   explicit, idempotent `migrate_db()` hooked into `monitor.py` startup, run with the bot
   stopped **or** under `PRAGMA busy_timeout` (both `monitor.py` and the ETA bot write the same
   SQLite file).

---

## Status snapshot

| Step | Name | State |
|------|------|-------|
| 3 | Route polyline snapping (map-matching) | ✅ **Done** — built, verified, awaiting deploy |
| 0 | Multi-stop arrival capture | ✅ **Done** — built, verified, awaiting deploy |
| 2 | PredictionLog (ETA drift logging) | ▶️ **Next** — additive, can start now |
| 1 | Empirical schedule (median observed arrival) | Planned — data-gated |
| 4 | Feature-engineering pipeline | Planned — data-gated |
| 5 | Baseline + XGBoost model | Planned — data-gated |

**Data reality:** ~hundreds of `stop_events` (the ML target). Steps 1/4/5 stay parked until
multi-stop capture (step 0) has multiplied and accumulated that count for several weeks.

---

## ✅ Step 3 — Route polyline snapping (DONE)

Snaps GPS pings and stops onto each direction's `shapes.txt` polyline so distance is measured
**along the road**, not straight-line.

- `src/analysis/map_matching.py` — `shape_for_route`, `project_point`,
  `distance_along_for_stop`, `route_distance_to_stop`, `ordered_stops_for_route`. mtime-cached,
  fail-soft (never raises), no new dependencies.
- Integrated into `src/analysis/predict_eta.py` live ETA with a straight-line fallback
  (`[straight-line]` tag) for off-route pings.
- Verified (`tests/test_map_matching.py`): stops snap at ~0 m with **0 ordering inversions**;
  real pings track at **2.1 m median** cross-track; **no self-overlap** on either 37 km shape;
  removed a **median ~1.2 km (up to ~11 km)** straight-line optimism bias. Adversarial review: safe.

**Remaining:** `git push` → `/update`; watch live ETAs for a couple of days to confirm the
accuracy win before layering capture on top.

---

## ✅ Step 0 — Multi-stop arrival capture (DONE — built, verified, awaiting deploy)

**Built 2026-06-16.** `geofence.py` rewritten to along-route bracket capture; `schedule.py`
gained an mtime-cached `stop_times` index + `get_scheduled_arrivals_for_trip`; `map_matching.py`
caches `ordered_stops_for_route`; `models.py` added `stop_sequence` / `cross_track_m` / `method`
columns + a `(trip_id, stop_id)` unique index, applied by a new idempotent `migrate_db()` hooked
into `monitor.py` startup; the `check_geofence()` call is now wrapped in try/except.
Verified (`tests/test_multi_stop_capture.py`): synthetic ping-pair brackets exactly 3 stops with
strictly increasing arrivals + correct order + boundary semantics + dedup; **backfill replay of a
real 983-ping trip captured 41/43 route stops, zero duplicates, monotonic arrivals.** `migrate_db()`
tested on a copy of the live DB: 3 columns + unique index added, idempotent, 106 rows preserved.
**Remaining:** `git push` → `/update`; confirm `stop_events` grows ~40×/trip live.

**Goal.** Log a `StopEvent` for **every** stop on Route 90 that a bus crosses, instead of only
the single hard-coded pair (7604 / 5411). This is the linchpin: it multiplies the labelled
dataset ~40× per trip using pings already being collected.

**Why now.** Its value is purely time-accumulated — every day it isn't running is data lost
forever — and map-matching (step 3) just removed the blocker that made it produce poisoned data.

**Design** (generalize `src/analysis/geofence.py`):

- **Detection by direction, not trip.** Candidate stops come from
  `map_matching.ordered_stops_for_route(route_id)` (each stop with its along-route distance).
  This is keyed off the stable `route_id`, so a 12 h GTFS swap can't blank out an in-flight bus.
- **Crossing detection = along-route bracket.** For a vehicle's two latest pings `pos1` (older)
  → `pos0` (newer), snap both to along-route distances `a1`, `a0`. Any stop whose along-route
  distance `s` lies in `(a1, a0]` was crossed. Interpolate its arrival:
  `arrival = pos1.ts + ((s - a1) / (a0 - a1)) * (pos0.ts - pos1.ts)`.
  Because stops are ordered along the route, the interpolated times are **monotonic** — this is
  exactly what map-matching makes valid, and it replaces the old 100 m-core / 400 m-approach
  scenarios. Multiple stops per ping-pair is now correct, not a bug.
  - Assumes ~constant along-route speed between pings (fine for 30–40 s gaps; a known, improvable
    approximation — sharpen later with `is_stationary` / sub-segment speed).
- **Guards.** Skip emission when `a0 <= a1` (stationary or backward GPS jitter); skip pings whose
  `cross_track_m` exceeds the off-route threshold (unreliable projection); require a non-null
  `trip_id` (needed for the schedule lookup and dedup).
- **Dedup = `(trip_id, stop_id)`**, enforced both ways: query the trip's already-logged stop_ids
  into a set and skip them (avoid generating doomed inserts), **and** a DB `UNIQUE` index as the
  hard guarantee. Do **not** abandon the whole vehicle on a hit (the old code's bug) — only skip
  the specific stops already logged.
- **Schedule + delay.** For each crossing, `schedule.get_scheduled_arrival(trip_id, stop_id, ...)`
  (already generic over arbitrary stops) → `delay_seconds = arrival - scheduled`. Reuse the cached
  GTFS tables rather than re-reading the 6.4 MB CSV per stop.
- **Backward-compat note.** 7604 / 5411 keep being logged (they're in their trips' stop lists),
  but new rows use the along-route interpolation, which differs slightly from the old 2D method.
  Historical rows are untouched. Record the cutover date (and optionally a `method` column) so the
  empirical-schedule step can account for the methodology change if needed.

**Schema** (`src/db/models.py`, applied by a new `migrate_db()`):

- Add `stop_sequence` (Integer) to `StopEvent` — trip-relative order; useful ML feature.
- (Optional) `cross_track_m` / `interp_confidence` (Float) — per-crossing quality flag.
- `CREATE UNIQUE INDEX IF NOT EXISTS uq_stop_events_trip_stop ON stop_events(trip_id, stop_id)`.
- `migrate_db()` is idempotent (`PRAGMA table_info` guard → `ALTER TABLE ADD COLUMN` →
  `CREATE UNIQUE INDEX IF NOT EXISTS`), called from `monitor.py` startup **before** the loop. The
  existing 106 rows have unique `(trip_id, stop_id)` and no nulls, so the unique index builds
  cleanly with no dedup-first step. Run once with the bot stopped, or set
  `PRAGMA busy_timeout=30000` on the migration connection.

**Robustness** (`src/monitor.py`): wrap the `check_geofence()` call in its own `try/except` that
logs and continues, so one malformed stop in the heavier pass can't crash the whole monitor loop.

**Files touched:** `geofence.py` (rewrite the crossing core), `models.py` + a `migrate_db()`,
`monitor.py` (call `migrate_db()` + wrap geofence). Reuses `map_matching.py` as-is.

**Verification:**
- Unit test: feed a synthetic ping-pair that brackets 3 stops → assert 3 events with strictly
  increasing arrival times and correct stop order.
- Backfill check: replay a stored trip's pings → assert ~all of its ~43 stops captured once, no
  duplicates, monotonic arrivals.
- Live: after deploy, confirm `stop_events` grows ~40×/trip and 7604/5411 still logged.

**Acceptance:** a full Route 90 trip yields one clean, correctly-ordered `StopEvent` per stop it
passes; reruns add no duplicates; the monitor never crashes on a bad stop; the 10 s polling
cadence is unaffected (measure geofence wall-time; time-gate it to run less often than every
cycle if it grows materially).

---

## Step 2 — PredictionLog (start the clock early; can run alongside step 0)

**Goal.** Record the system's own ETA predictions so we can later measure prediction *drift*
(how wrong the ETA was N minutes before actual arrival) — the dataset needed to train on
system-level error.

**Why early.** Additive and cheap, and its value compounds with time just like step 0.

**Design.**
- New `PredictionLog` table: `vehicle_id, trip_id, route_id, stop_id, predicted_arrival,
  predicted_at, lead_time_min, eta_source (schedule|move|hybrid), distance_m, speed_kmh,
  is_stationary`. Create via `migrate_db()` (`CREATE TABLE IF NOT EXISTS`).
- `monitor.py` hook (time-gated, e.g. once/minute): for each active Route 90 bus with an ETA,
  snapshot the prediction. Reuse the ETA logic from `predict_eta.py` (factor the per-vehicle
  computation into a callable so the bot and the logger share one source of truth).
- Analysis later: join `PredictionLog` → `StopEvent` on `(trip_id, stop_id)` to get
  `predicted_arrival` vs `actual_arrival` bucketed by `lead_time_min`.

**Acceptance:** every active bus produces periodic prediction rows that join cleanly to the
eventual actual arrival.

---

## Step 1 — Empirical schedule (data-gated)

**Goal.** Replace the official (physically-impossible) timetable with the **median observed
arrival**, so delay is measured against a grounded baseline instead of a fictional one.

**Design.** `src/analysis/generate_empirical_schedule.py` aggregates `stop_events`. Because
`trip_id`s rotate, **aggregate by `(stop_id, day_type, scheduled-time-of-day / headway slot)` —
never by `trip_id` across days.** For each cell, compute the median observed arrival → an
empirical timetable. Recompute `delay = actual_arrival − empirical_median`.

**Gate / trigger:** enough observations per cell (rule of thumb ≥ ~10). Multi-stop capture
(step 0) reaches this far faster than single-stop did. Re-evaluate ~3–4 weeks after step 0 deploys.

**Acceptance:** an empirical timetable per stop/day-type with sane medians and enough support;
delay distributions visibly less biased than against the official schedule.

---

## Step 4 — Feature-engineering pipeline (data-gated)

**Goal.** `src/analysis/prepare_training_data.py` builds the training table by merging:
- **Target:** empirical delay (from step 1).
- **Temporal:** cyclically-encoded time-of-day (sin/cos), `day_type` (weekday / weekend / public
  holiday).
- **Upstream:** `terminal_departure_delay_seconds` (from `TripSummary`, already collected).
- **Environmental:** `is_raining`, `temperature_c`, `wind_speed_kmh` (from `WeatherRecord`),
  joined on nearest timestamp.
- **Drift:** historical error rates from `PredictionLog` (step 2).
- **Spatial:** `stop_sequence` / along-route position (from step 0).

**Gate:** depends on steps 0–2 having accumulated data and step 1's empirical target existing.

**Acceptance:** a clean, leakage-free feature matrix with the empirical-delay target.

---

## Step 5 — Baseline + XGBoost (data-gated)

**Goal.** Train the "historical median" baseline first, then an **XGBoost regressor** on the
step-4 table; surface **prediction intervals** ("arriving in 6–9 min") rather than false-precision
point estimates. Update `predict_eta.py` to query the model.

**Gate:** ≥ ~few thousand labelled `stop_events`. Building a gradient-boosted model on a few
hundred rows would overfit and is portfolio-negative — do not start before the volume exists.

**Acceptance:** XGBoost beats the median baseline on held-out (time-split, not random) data;
feature importances are sane; intervals are calibrated.

---

## Data-gating triggers (summary)

| Step | Unblock condition |
|------|-------------------|
| 0 — capture | None — build now (foundation in place) |
| 2 — PredictionLog | None — build now (additive) |
| 1 — empirical schedule | ≥ ~10 obs per `(stop, day_type, slot)` cell (≈ weeks after step 0) |
| 4 — feature pipeline | steps 0–2 accumulating + step 1 target exists |
| 5 — XGBoost | ≥ ~few thousand labelled `stop_events` |

---

## Cross-cutting invariants (don't violate)

- **Dedup key is `(trip_id, stop_id)`** — trip_ids don't recur, so no `service_date`.
- **Schema changes go through an idempotent `migrate_db()` hooked into `monitor.py` startup** —
  never assume `init_db()`/`create_all` runs. Mind the two concurrent SQLite writers
  (`monitor.py` + the reactive-fetch ETA bot): run migrations with the bot stopped or under
  `PRAGMA busy_timeout`.
- **Detection keys off `route_id` (stable), `trip_id` only for schedule/dedup** (gracefully null).
- **`Config.BASE_DIR` = project root** (`data/bus_data.db`, `data/raw/static/Limassol/`).
- **Map-matching/geofence functions fail soft** — a missing shape or bad coord returns
  None/ok=False and falls back; it never crashes a caller.
- **Self-contained project root** — all paths relative/auto-detected; nothing written outside the
  project folder (host may relocate it).

---

## Out of scope (deliberately not building)

- **Multi-city / other-route expansion** — scope bloat; the value is one route + a deep, clean
  dataset. Widening *stops on Route 90* is good; widening *cities* is not.
- **A web/live-map frontend** — betrays the low-end-device, text-on-Telegram audience; the
  differentiator is accurate text predictions, not a prettier map.
- **Training any model on the official-schedule delay** — it's a fictional baseline; wait for the
  empirical schedule (step 1).
