# Cyprus Bus Analysis Project

## Purpose
The primary purpose of this project is to build a robust data engineering and predictive modeling system for public transit in Cyprus. Rather than relying on frontend interfaces like `busonmap.com`, this system directly ingests official General Transit Feed Specification (GTFS) and GTFS-Realtime (GTFS-RT) data from the Traffic4Cyprus portal to empirically calculate bus delays and early arrivals.

**Current Primary Target:**
*   **City:** Limassol
*   **Route:** 90
*   **Stop:** Panagia Pyrgiotissa Church 1 (Public ID: 11636, Internal ID: 7604)
*   **Coordinates:** Lat 34.7416229691767, Lon 33.1836621951358

## Architecture (Restructured)
The system is built in Python using a modular architecture, significantly improved for ML readiness:

### 1. Ingestion Layer (`src/ingestion/`)
*   **`fetch_static.py`**: Downloads static GTFS zip files (timetables, stops, routes) autonomously. Now implements **MD5 Hashing** to detect silent schedule changes by the transit authority, preventing training data corruption.
*   **`fetch_rt.py`**: Connects to the GTFS-RT Protocol Buffer feed every 10 seconds. Now includes **Stationary Detection**, calculating distance from previous pings to flag buses stopped at traffic lights or terminals, preventing "phantom movement" during interpolation.
*   **`fetch_weather.py`**: Automatically queries the Open-Meteo API hourly to record temperature, wind speed, and precipitation for contextual ML features.

### 2. Storage Layer (`src/db/`)
*   **`models.py`**: Defines the SQLAlchemy ORM schema.
    *   `VehiclePosition`: Logs GPS pings (with `is_stationary` flags).
    *   `StopEvent`: Logs calculated arrivals with interpolated timestamps.
    *   `ScheduleVersion`: Tracks GTFS file hash updates.
    *   `TripSummary`: Stores upstream features like `terminal_departure_delay_seconds`.
    *   `WeatherRecord`: Stores periodic weather context.
*   **Database:** Currently utilizes a local SQLite database (`data/bus_data.db`) with active preparations (via GeoAlchemy2 and psycopg) to migrate to PostgreSQL + PostGIS for spatial indexing at scale.

### 3. Analysis Engine (`src/analysis/`)
*   **`schedule.py`**: Utilizes `pandas` to efficiently query static `stop_times.txt` files for scheduled arrivals.
*   **`geofence.py`**: The spatial engine. Upgraded from a strict 100m geofence to a **400m Approach Zone** with time-proportionate interpolation to precisely estimate stop crossings. Also calculates "Birth Delays" (terminal departure delay) the first time a trip is observed.
*   **`predict_eta.py`**: Calculates moving averages, factors in dead-reckoning, and formats the live status for Telegram.

### 4. Orchestration (`src/monitor.py` & `.bat` files)
*   **`monitor.py`**: An infinite loop orchestrator. Now handles 10-second live polling, 1-hour weather polling, and 12-hour automated schedule updates natively.

---

## Evolution Plan & Implemented Phases

### Phase 1: Robust Data Foundation (COMPLETED)
*   **Problem Addressed:** The "Illusion of Precision" caused by 30-40s GPS ping intervals vs. a strict 100m geofence, and the inability to distinguish slow vs. stopped vehicles.
*   **Solutions Implemented:**
    *   **Stationary Flag:** Added `is_stationary` logic based on <30m movement between pings.
    *   **Geofence Interpolation:** Expanded detection to 400m and mathematically interpolates the exact second a bus crosses the 100m threshold, solving missed "pass-by" events.
    *   **Postgres Readiness:** Installed `GeoAlchemy2` and `psycopg` to prepare for scaling out of SQLite.

### Phase 2: Data Richness & Pipeline Integrity (COMPLETED)
*   **Problem Addressed:** Training ML models on silently updated schedules, and lacking "upstream" visibility (ignoring why a bus is late).
*   **Solutions Implemented:**
    *   **GTFS Hashing:** Implemented automated MD5 hash checks in `fetch_static.py` to version control the timetables (`ScheduleVersion` table).
    *   **Birth Delay Tracking:** Added `TripSummary` logic to `geofence.py`. When a bus is first spotted near its starting terminal, the system calculates and permanently logs its initial departure delay, creating a powerful predictive feature.
    *   **Auto-Update Orchestration:** Made `monitor.py` completely autonomous, handling its own schedule and weather updates without human intervention.
    *   **Weather Integration:** Added `fetch_weather.py` and `WeatherRecord` table to continually collect Open-Meteo data (Rain, Wind, Temp) every hour.
    *   **Telegram Reactive Polling:** Upgraded the ETA bot to trigger an immediate, on-demand GTFS-RT fetch upon receiving a message, bypassing any local DB lag.
    *   **Automated Crash Alerts:** Integrated a secondary Telegram bot within `monitor.py`'s exception handler to instantly notify the administrator of any fatal pipeline crashes.

### Phase 3: Empirical Baselines & Machine Learning (UPCOMING)
*   **Objective:** Transition from math-based interpolation and schedule reliance to empirically backed predictions using Machine Learning (XGBoost/LightGBM).

**Detailed Implementation Plan for Phase 3:**
1.  **Empirical Schedule Generation:**
    *   **Problem:** The official GTFS schedule is often physically impossible. Calculating delay against a "fictional" schedule introduces permanent systematic bias.
    *   **Action:** Build an analytics script (`generate_empirical_schedule.py`) that aggregates months of `StopEvent` data. For every `(stop_id, trip_id, day_of_week)` combination, calculate the **median observed arrival time**.
    *   **Outcome:** Delay will be recalculated as `actual_arrival - empirical_median_arrival`, giving the ML model a grounded target to predict.

2.  **Prediction Snapshot Logging (Delta Analysis):**
    *   **Problem:** The system doesn't record its own past estimations, making it impossible to train a model on "prediction drift" (how wrong the system was X minutes before actual arrival).
    *   **Action:** Create a `PredictionLog` database table and update `monitor.py` to auto-log ETAs at fixed intervals (e.g., 5, 10, 15 minutes before expected arrival).
    *   **Outcome:** Creates a direct comparative dataset (`predicted_arrival` vs `actual_arrival` from `StopEvent`) to train the ML model on system-level prediction errors.

3.  **Route Polyline Snapping (Map Matching):**
    *   **Problem:** Linear dead-reckoning assumes buses drive in straight lines through buildings.
    *   **Action:** Integrate `shapes.txt` from the static GTFS. Snap raw GPS coordinates to the route's specific polyline geometry. Interpolate distance along the actual road rather than 2D space.
    *   **Outcome:** Vastly more accurate distance-to-stop calculations, especially around corners and complex urban junctions.

4.  **Feature Engineering Consolidation:**
    *   **Action:** Create a data preparation pipeline (`prepare_training_data.py`) that merges:
        *   The target variable: Empirical Delay.
        *   Temporal features: Cyclically encoded time-of-day (sine/cosine), `day_type` (weekday, weekend, public holiday).
        *   Upstream context: `terminal_departure_delay_seconds` (from Phase 2).
        *   Environmental context: `is_raining`, `temperature_c` (from `WeatherRecord`).
        *   Prediction Deltas: Historical error rates merged from the `PredictionLog` table.

5.  **Baseline Model & XGBoost Deployment:**
    *   **Action:** Train the "Historical Median Model" as a strict baseline.
    *   **Action:** Train an **XGBoost Regressor** on the tabular dataset created in Step 4. XGBoost natively handles missing values and provides clear feature importance.
    *   **Outcome:** Update `predict_eta.py` to query the trained XGBoost model instead of relying solely on schedule logic and moving averages. Introduce **Prediction Intervals** (e.g., "Arriving in 6–9 minutes" instead of a confident "7 minutes").

---

## Recent Fixes & Deployments (April 7, 2026)
*   **Hetzner Server Deployment:** Successfully migrated the entire monitoring pipeline (monitor, SQLite DB, Telegram bot) to a 24/7 Hetzner Ubuntu server. Configured native timezone settings to fix EEST vs UTC timezone collision bugs with local data uploads.
*   **Phantom Movement Fix:** Stopped `predict_eta.py` from extrapolating movement for stale (>120s) or stationary pings, fixing an issue where stopped buses artificially "moved" towards the stop. Added `[STALE DATA]` and `[STATIONARY]` tags to the bot UI.
*   **Ghost Trip Safeguard:** Filtered out active pings for vehicles that have no scheduled trips in the GTFS timetable, preventing the bot from tracking off-duty vehicles purely based on movement.
*   **Optimized Geofencing:** Updated `geofence.py` to only process active vehicles from the last 3 hours, removing log spam from multi-day-old stale pings.
*   **Static Schedule Fallback:** When no active buses are found, the Telegram bot now automatically scans `stop_times.txt` and `calendar_dates.txt` to display the very next scheduled departure times for both directions.    

---

## Current Task Tracking: Phase 3 Preparation (April 7, 2026)
**Objective:** With the pipeline successfully running on the Hetzner server, the system is now in an active data-collection phase. The next immediate tasks involve building the structural components of Phase 3 while we wait to accumulate a statistically significant month of data.

### Progress Checklist (Next Steps):
- [ ] **Infrastructure:** Execute PostgreSQL + PostGIS database migration on the Hetzner server using `migrate_db.py` to prepare for spatial indexing.
- [ ] **Data Pipeline:** Build the `PredictionLog` schema and background ETA logging logic.
- [ ] **Data Pipeline:** Build the `prepare_training_data.py` script to successfully join weather, delay, upstream, and prediction delta data into a tabular format.
- [ ] **Spatial Logic:** Begin developing the Route Polyline Snapping script using `shapes.txt` to test accurate on-road distance calculations.
- [ ] **Data Collection:** Allow the server to run continuously for ~4 weeks to capture weekly seasonality, weather patterns, and sufficient empirical schedules.
### Technical Notes:
*   **Towards Lemesos Stop:** `5411`
*   **Towards Sanida Stop:** `7604`
*   **Route ID (Lemesos):** `10900011`
*   **Route ID (Sanida):** `10900012`

---

## Server Deployment Architecture (Windows 11 Pull-Based CI/CD)
The project has been migrated from a Linux Hetzner server to a dedicated **Windows 11** system. To allow remote updates via Telegram without requiring direct access to the Windows PC, the system utilizes a Git-based Pull CI/CD architecture.

1. **`COMPLETE_LAUNCH.bat`:** A single master batch script that launches all components in separate, titled Windows Command Prompts:
   * **Data Orchestrator** (`run_monitor.bat`)
   * **Public ETA Bot** (`start_telegram_bot.bat`)
   * **Update Manager** (`src/admin_bot.py`)
2. **`admin_bot.py` (The Update Manager):** A lightweight, always-on Telegram bot running locally on the Windows server that listens for deployment commands and natively manipulates Windows processes using `taskkill`.

**The New Development Workflow:**
1. Code changes (bug fixes, new ML features) are written locally on the developer's laptop.
2. Changes are committed and pushed to GitHub: `git add .` -> `git commit -m "Update"` -> `git push`
3. The developer sends the `/update` command to the Admin Telegram Bot.

**Telegram Commands (Admin Only):**
*   `/update`: Performs a "Nuke and Pave" update (`git fetch`, `git reset --hard origin/main`), runs `pip install -r requirements.txt`, uses Windows `taskkill` to cleanly close the existing bot/monitor windows, and relaunches them via `COMPLETE_LAUNCH.bat`.
*   `/rollback`: Reverts the server's codebase to the previous commit (`git reset --hard HEAD~1`) and restarts the processes.
*   `/status`: Checks if the admin bot is online.

**Server Setup Requirements (One-Time on Windows 11 PC):**
1. **Clone Repo:** Install Git, then `git clone https://github.com/fedor5556/Cyp_Bus_Bot.git`.
2. **Environment File:** Create a `.env` file containing `ADMIN_TELEGRAM_ID` and `ADMIN_BOT_TOKEN` in the project root. Transfer this securely; it is explicitly ignored by git.
3. **Data Transfer (CRITICAL):** Because the SQLite database operates in high-performance Write-Ahead Logging (WAL) mode, you MUST manually copy all three database files from the Linux server to the Windows `data/` folder to prevent severe data loss:
   * `bus_data.db` (Main database)
   * `bus_data.db-wal` (Recent unmerged writes)
   * `bus_data.db-shm` (Shared memory index)
4. **Launch:** Run `COMPLETE_LAUNCH.bat`. The Admin Bot will handle all future updates remotely.