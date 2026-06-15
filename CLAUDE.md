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
*   **`monitor.py`**: An infinite loop orchestrator. Now handles 10-second live polling, 1-hour weather polling, 12-hour automated schedule updates, 12-hour cloud DB backups, and a 10-minute-retry transcriber `.env` restore — all natively.

### 5. Cloud Transfer Layer (`src/cloud_sync.py`) — added June 11, 2026
*   **Provider:** Backblaze B2 (private bucket `cyprus-bus-bot`, 10 GB free tier, no payment card required). Originally designed for GCS; ported because GCP billing requires a card.
*   **Interface:** `is_configured()` / `pull()` / `push()` / `push_db_backup()` — provider-agnostic signatures, fail-safe (never raises into callers), lazy `b2sdk` import (module loads even if the SDK or key is absent), atomic `.part`→`os.replace` writes, no-clobber + content-marker validation, SQLite online-backup snapshots.
*   **The only secret** is a bucket-scoped B2 application key stored gitignored at `secrets/b2_key.json`; it is delivered out-of-band via Telegram (never git). The bucket *name* is committed in `config.py` (not a secret — the bucket is private and key-scoped).
*   **Purpose:** restores the inbound file channel lost when the in-project admin bot was replaced by the send-only Admin Hub, and provides a large-file path (DB backups out, ML models in) beyond Telegram's ~20 MB inbound limit.

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

## Recent Fixes & Deployments (June 11, 2026)
*   **Backblaze B2 cloud transfer layer (bus repo `ba1d0e2`, deployed + armed):** `src/cloud_sync.py` + config (`Config.ADMIN_IDS`, `B2_BUCKET`, `B2_KEY_PATH`). Automated 12-hour DB backups to `bus-backups/` are live.
*   **Inbound Telegram file channel on the public ETA bot** (admin-gated by numeric ID, fail-closed, silent to non-admins): DM a `.env` document with the target project as caption/filename (alias `transcriber`, `bus`, or any exact sibling folder name; previous file kept as `.env.bak`) — this is how the transcriber's missing `.env` was finally delivered, fixing its outage. Also `/armb2 <keyID> <appKey>` (stores the B2 key, scrubs the message), `/pushdb` (on-demand backup), `/pull <object>` (downloads a bucket object to `src/models/` — replaces the lost `/upload_model`).
*   **Telegram 409 fix:** `predict_eta.py` now uses `run_polling(drop_pending_updates=True)` plus a localhost-port single-instance guard, so a surviving old poller can no longer silently deafen the bot.
*   **Transcriber hardening (transcriber repo `15c6a5f`, deployed):** fail-closed `ALLOWED_USERS` allowlist (mixed `@usernames` + numeric IDs) gating both media intake and the Plain/Summarize callbacks, protecting the Groq API quota. Missing/empty variable = refuse everyone.
*   **Dependency hygiene:** `requirements.txt` re-frozen from the working venv (added `b2sdk`, corrected the hand-edited `GeoAlchemy2` pin drift to the actually-installed `0.18.3`).
*   **Pending (B2 console, manual):** lifecycle rule on prefix `bus-backups/` (hide after 30 days, delete 1 day later) so backups don't outgrow the 10 GB free tier (~4 months at current DB size).

---

## Current Task Tracking: Phase 3 Preparation (June 3, 2026)
**Objective:** Pipeline successfully migrated from Hetzner to friend's Windows 11 PC. System is in active data-collection phase. Next steps: build Phase 3 structural components while accumulating data.

**Server Status (as of June 3, 2026):**
*   GPS Pings: **196,232** rows
*   Stop Events: **427** rows
*   DB Size: **42.5 MB**
*   Platform: Windows 11 (friend's PC)

### Progress Checklist (Next Steps):
- [x] **Infrastructure:** Migrate pipeline from Hetzner to friend's Windows 11 PC.
- [x] **Infrastructure:** Set up Git-based CI/CD (standalone Admin Hub controls all sibling projects).
- [x] **Infrastructure:** ~~Telegram-based data sync (`/backup`, `/restore`, `/upload_model`)~~ → superseded June 11, 2026 by the B2 cloud layer + bus-bot inbound channel (`/pushdb`, `/pull`, `.env` DM).
- [x] **Infrastructure:** Backblaze B2 transfer layer deployed and armed; automated 12 h DB backups live.
- [ ] **Data Pipeline:** Build the `PredictionLog` schema and background ETA logging logic.
- [ ] **Data Pipeline:** Build the `prepare_training_data.py` script to join weather, delay, upstream, and prediction delta data.
- [ ] **Spatial Logic:** Begin developing the Route Polyline Snapping script using `shapes.txt`.
- [ ] **Data Collection:** Allow the server to run continuously for ~4 weeks to capture weekly seasonality.
### Technical Notes:
*   **Towards Lemesos Stop:** `5411`
*   **Towards Sanida Stop:** `7604`
*   **Route ID (Lemesos):** `10900011`
*   **Route ID (Sanida):** `10900012`

---

## Server Deployment Architecture (Windows 11 Pull-Based CI/CD)
The project was originally deployed on a Linux Hetzner server (Feb–Jun 2026 for data collection) and has been migrated to a dedicated **Windows 11 PC** hosted by a friend for 24/7 operation.

> **CRITICAL HOST REQUIREMENT:** The entire project MUST remain self-contained within a single root folder (e.g., `C:\CypBusBot`). The host may relocate this folder at any time. All paths must be relative or auto-detected. No files should be written outside the project root.

### Three-Machine Architecture (updated June 11, 2026)

```
Developer PC ──git push──> GitHub ──/update (Admin Hub)──> Friend's Win11 PC (Server)
     ^                                                          │  ^
     │            Backblaze B2 private bucket (cyprus-bus-bot)  │  │
     └── download backups ◄── bus-backups/ ◄── 12h auto-push ───┘  │
         upload models/.env ──► bucket ──► /pull or 10-min pull ───┘
```

1.  **Developer PC:** Code editing, ML training (heavy compute). Pushes code to GitHub via `push.bat`.
2.  **GitHub:** Code-only **public** repositories (one per project). No data, no secrets, no large models. Acts as the relay.
3.  **Friend's Win11 PC:** 24/7 server hosting three sibling projects (`Cyp_Bus_Bot`, `Admin_hub`, `Constan_transcriber_telegram_bot`) under one `Projects\` root. No SSH, no inbound ports — reachable only via Telegram bots (outbound HTTPS) and git.
4.  **Backblaze B2** (private bucket, key-scoped): the large-file / secrets side-channel that public git and the send-only Admin Hub cannot provide.

### Process Architecture
Two bus processes run on the server (the old in-project "Admin Deployment Bot" was removed — control moved to the standalone **Admin Hub**, a sibling project with its own repo, which also supervises launches via `Admin_hub/runner.py`):
   * **Bus Monitor** → `src/monitor.py` (polling, geofence, weather, schedule updates, cloud backups)
   * **Public ETA Bot** → `src/analysis/predict_eta.py --bot` (public ETAs + the admin-gated inbound file channel)

### Remote Control: Two Telegram Bots with Distinct Roles

**1. Admin Hub (standalone sibling project — SEND-ONLY, never receives files):**
*   Per project: `/update` (nuke-and-pave: `git fetch` + `git reset --hard origin/main` + venv pip install + kill + relaunch), `/rollback`, ▶️ Start / 🛑 Stop / 🔄 Restart, `/status`, `/logs`, `/dbstats`, `/backup` (sends DB ZIP out, while it still fits Telegram's ~50 MB send cap).
*   **Rule: the Hub is the only management lifeline and must never be modified** to add inbound capability — that's what the bus bot's channel below is for.
*   **`/update` vs Restart:** only Update pulls new code from GitHub; Restart just relaunches what's already on disk.

**2. Cyprus Bus Bot — admin-gated inbound channel (added June 11, 2026; all admin-only, numeric-ID gated, fail-closed, silent to non-admins):**
*   **DM a `.env` document** with the target project as the caption (alias `transcriber`, `bus`, or any exact sibling folder name; or name the file `<target>.env`): validated (must contain `KEY=value` lines, ≤64 KB), previous file kept as `.env.bak`, written atomically. Restart that project via the Hub to apply. *This is the standard way to deliver/rotate any project's secrets.*
*   `/armb2 <keyID> <applicationKey>`: stores the B2 application key at `secrets/b2_key.json` and deletes the message to scrub the secret. (A DM'd key `.json` works too.)
*   `/pushdb`: on-demand DB snapshot (SQLite online-backup API) → uploads to `bus-backups/` in the bucket.
*   `/pull <object-name>`: downloads a bucket object into `src/models/` — replaces the old `/upload_model` for large ML models.

### Security Model
*   **Admin authentication:** every admin command and file handler gates on the sender's **numeric** Telegram user ID against `ADMIN_TELEGRAM_ID` (comma-separated). Numeric IDs are server-assigned and unforgeable; usernames are never used for admin gates. Missing env var = fail closed. Non-admins get silence from the inbound handlers (the ETA bot stays public for ETA queries only).
*   **Transcriber access control:** fail-closed `ALLOWED_USERS` allowlist (mixed `@usernames` + numeric IDs; IDs preferred — a freed username can be re-claimed by a stranger) gates all Groq-spending paths.
*   **Secrets routing:** bot tokens / API keys live in gitignored `.env` files, delivered via the bus bot's `.env` DM channel (or the original one-time setup ZIP) — never via the public repos. The B2 application key is bucket-scoped (Read & Write on `cyprus-bus-bot` only), stored gitignored, rotatable remotely via `/armb2`.
*   **Current authorized admins:** 2 users (developer + friend).
*   **No open ports:** no SSH, no web server; all communication is outbound HTTPS (Telegram + B2 APIs).

### Data Sync Workflow (B2-based, June 11, 2026)
Because GitHub blocks files >100 MB, repos are public (no secrets), and Telegram bots can only *receive* ~20 MB:
1.  **Getting data for ML training:** automatic — the monitor pushes a DB snapshot to `bus-backups/` every 12 h (plus `/pushdb` on demand) → download from the B2 console. Telegram `/backup` still works while the zipped DB fits ~50 MB.
2.  **Pushing small models (<100 MB):** commit to `src/models/` → `git push` → `/update`. (Note: `src/models/*.pkl|*.joblib|*.json` are gitignored — large/binary models should use the B2 path.)
3.  **Pushing large models:** upload to the bucket (B2 console) → `/pull <object>` to the bus bot → lands in `src/models/`.
4.  **Delivering/rotating secrets:** DM the `.env` to the bus bot with the project caption → Hub restart. Fallback self-heal: if the transcriber's `.env` is ever missing, the monitor restores it from bucket object `transcriber/.env` (if uploaded) within 10 minutes.
5.  **Retention:** a B2 lifecycle rule on `bus-backups/` (hide after 30 days) keeps storage inside the 10 GB free tier.

### Initial Server Setup (One-Time)
The setup is automated via `setup/SETUP.bat` included in a ZIP package:
1.  Friend receives ZIP containing `SETUP.bat`, `SETUP_README.txt`, and `server_data/` (with `.env` + DB files).
2.  Friend unzips and double-clicks `SETUP.bat`.
3.  Script validates Python/Git → clones repo → copies `.env` + DB → creates venv → installs deps → launches `COMPLETE_LAUNCH.bat`.
4.  All future updates are handled remotely via `/update` command.