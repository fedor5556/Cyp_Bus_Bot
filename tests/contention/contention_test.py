"""
Real cross-process SQLite lock-contention verification for finding #1
(WAL + busy_timeout applied on the runtime db.models.get_engine path).

This spawns several REAL OS processes (subprocess, NOT threads) that hammer the
SAME SQLite DB file concurrently -- the true analogue of monitor.py and
predict_eta.py running as two separate `python` processes against bus_data.db.

It measures SQLITE_BUSY ("database is locked") errors under three configs:

  new : the production path -> db.models.get_engine() (singleton, journal_mode=WAL,
        busy_timeout=30000). The fix. Expect ZERO lock errors and unblocked reads.
  old : the pre-fix runtime path -> plain create_engine(url) (default DELETE
        journal, pysqlite's default 5 s busy timeout). No PRAGMAs.
  raw : DELETE journal + busy_timeout=0 -> CONTROL that exposes the raw collision
        rate, proving the workload genuinely contends (otherwise "new = 0 errors"
        would be unfalsifiable / could just mean no overlap happened).

Workers:
  * writers loop INSERTing into vehicle_positions as fast as possible
  * readers loop SELECT COUNT(*) as fast as possible (reader-vs-writer is where
    WAL matters most: in DELETE journal a committing writer blocks readers).

Run:
  python tests/contention/contention_test.py --mode raw
  python tests/contention/contention_test.py --mode old
  python tests/contention/contention_test.py --mode new
  python tests/contention/contention_test.py --mode all   (default)

Exit code 0 if the verdict holds (raw shows contention, new shows zero lock
errors); 1 otherwise.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(REPO_ROOT, "src")
sys.path.insert(0, SRC)

from sqlalchemy import create_engine, event, text  # noqa: E402

WRITERS = 4
READERS = 2
DURATION = 6.0          # seconds of hammering
START_DELAY = 2.0       # seconds for all workers to spin up before the synced start


# --------------------------------------------------------------------------- #
# Engine construction per mode
# --------------------------------------------------------------------------- #
def make_engine(mode, db_url):
    """Return an Engine for the given mode. `new` goes through the REAL production
    get_engine() so the test exercises the exact code shipped, in a separate process."""
    if mode == "new":
        # Exercise the shipped code path verbatim: DATABASE_URL is already set in env,
        # config read it at import, get_engine() builds the singleton + connect listener.
        os.environ["DATABASE_URL"] = db_url
        from db.models import get_engine
        return get_engine()

    engine = create_engine(db_url, echo=False)

    if mode == "raw":
        # Strip the safety net: DELETE journal + busy_timeout=0 -> a write collision
        # raises SQLITE_BUSY instantly instead of waiting. This is the control that
        # proves the workload actually contends.
        @event.listens_for(engine, "connect")
        def _raw_pragmas(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=0")
            cur.execute("PRAGMA journal_mode=DELETE")
            cur.close()
    elif mode == "old":
        # The literal pre-fix runtime path: plain create_engine. pysqlite's default
        # busy timeout is 5 s and the journal is DELETE. No listener at all.
        pass
    else:
        raise SystemExit(f"unknown mode {mode!r}")

    return engine


def probe_pragmas(engine):
    with engine.connect() as conn:
        jm = conn.execute(text("PRAGMA journal_mode")).scalar()
        bt = conn.execute(text("PRAGMA busy_timeout")).scalar()
    return str(jm).lower(), int(bt)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def run_worker(args):
    mode, db_url, kind, wid, start_epoch, duration = (
        args.mode, args.db_url, args.kind, args.id, args.start, args.duration)

    engine = make_engine(mode, db_url)
    journal_mode, busy_timeout = probe_pragmas(engine)

    attempts = successes = lock_errors = other_errors = 0
    max_latency_ms = 0.0

    # Busy-wait to the synchronized start so every process overlaps.
    while time.time() < start_epoch:
        time.sleep(0.001)

    end = start_epoch + duration
    while time.time() < end:
        attempts += 1
        t0 = time.perf_counter()
        try:
            if kind == "writer":
                with engine.begin() as conn:
                    conn.execute(
                        text("INSERT INTO vehicle_positions "
                             "(vehicle_id, route_id, latitude, longitude, timestamp) "
                             "VALUES (:v, :r, :lat, :lon, :ts)"),
                        {"v": f"W{wid}", "r": "10900012", "lat": 34.7, "lon": 33.1,
                         "ts": datetime(2026, 6, 16, 12, 0, 0)},
                    )
            else:  # reader
                with engine.connect() as conn:
                    conn.execute(text("SELECT COUNT(*) FROM vehicle_positions")).scalar()
            successes += 1
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                lock_errors += 1
            else:
                other_errors += 1
        finally:
            lat_ms = (time.perf_counter() - t0) * 1000.0
            if lat_ms > max_latency_ms:
                max_latency_ms = lat_ms

    result = {
        "kind": kind, "id": wid, "mode": mode,
        "attempts": attempts, "successes": successes,
        "lock_errors": lock_errors, "other_errors": other_errors,
        "max_latency_ms": round(max_latency_ms, 1),
        "journal_mode": journal_mode, "busy_timeout": busy_timeout,
    }
    print(json.dumps(result))
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def build_schema(db_url):
    """Create the real schema with a PLAIN engine so the file starts in DELETE
    journal mode (mode 'new' will convert it to WAL itself, exactly like the live
    monitor does on first run)."""
    os.environ["DATABASE_URL"] = db_url
    from db.models import Base
    eng = create_engine(db_url, echo=False)
    Base.metadata.create_all(eng)
    eng.dispose()


def run_mode(mode, db_path):
    db_url = "sqlite:///" + db_path.replace("\\", "/")

    # Fresh DB file (+ wipe any WAL/SHM sidecars from a prior mode) so each mode is clean.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    build_schema(db_url)
    if mode == "new":
        # Pre-convert to WAL offline (uncontended) -- exactly the step recommended
        # for the live DB before restart -- so this run measures STEADY-STATE WAL
        # concurrency, not the one-time startup conversion stampede. (That race, and
        # that the hardened listener survives it, is covered by wal_conversion_race.py.
        # Without this, simultaneously-starting workers race the DELETE->WAL convert
        # and each probe a transient delete/wal mix even though 0 lock errors occur.)
        import sqlite3 as _sq
        _c = _sq.connect(db_path)
        _c.execute("PRAGMA journal_mode=WAL")
        _c.close()

    start_epoch = time.time() + START_DELAY
    procs = []
    specs = [("writer", i) for i in range(WRITERS)] + [("reader", i) for i in range(READERS)]
    for kind, i in specs:
        cmd = [sys.executable, os.path.abspath(__file__), "--role", "worker",
               "--mode", mode, "--db-url", db_url, "--kind", kind, "--id", str(i),
               "--start", repr(start_epoch), "--duration", str(DURATION)]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))

    results, errs = [], []
    for p in procs:
        out, err = p.communicate(timeout=DURATION + START_DELAY + 60)
        if err.strip():
            errs.append(err.strip())
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    errs.append("unparseable worker line: " + line[:80])
    return results, errs


def summarize(mode, results, errs):
    writers = [r for r in results if r["kind"] == "writer"]
    readers = [r for r in results if r["kind"] == "reader"]

    def agg(group, field):
        return sum(r[field] for r in group)

    w_succ, w_lock = agg(writers, "successes"), agg(writers, "lock_errors")
    r_succ, r_lock = agg(readers, "successes"), agg(readers, "lock_errors")
    other = agg(results, "other_errors")
    jm = {r["journal_mode"] for r in results}
    bt = {r["busy_timeout"] for r in results}
    r_max_lat = max((r["max_latency_ms"] for r in readers), default=0.0)

    print(f"\n=== mode: {mode} ===")
    print(f"  journal_mode seen by workers: {sorted(jm)}   busy_timeout: {sorted(bt)}")
    print(f"  WRITERS ({len(writers)}): commits={w_succ:<6} lock_errors={w_lock}")
    print(f"  READERS ({len(readers)}): reads={r_succ:<8} lock_errors={r_lock}  max_read_latency={r_max_lat} ms")
    print(f"  other (non-lock) errors: {other}")
    if errs:
        print(f"  [stderr from workers] {errs[:2]}")

    return {
        "mode": mode, "journal_mode": sorted(jm), "busy_timeout": sorted(bt),
        "writer_commits": w_succ, "writer_lock_errors": w_lock,
        "reader_reads": r_succ, "reader_lock_errors": r_lock,
        "reader_max_latency_ms": r_max_lat, "other_errors": other,
        "n_results": len(results), "n_errs": len(errs),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="orchestrator")
    ap.add_argument("--mode", default="all")
    ap.add_argument("--db-url", dest="db_url")
    ap.add_argument("--kind")
    ap.add_argument("--id", type=int)
    ap.add_argument("--start", type=lambda s: float(s))
    ap.add_argument("--duration", type=float)
    args = ap.parse_args()

    if args.role == "worker":
        run_worker(args)
        return

    modes = ["raw", "old", "new"] if args.mode == "all" else [args.mode]
    tmpdir = tempfile.mkdtemp(prefix="bus_contention_")
    db_path = os.path.join(tmpdir, "contention.db")
    print(f"Cross-process contention test")
    print(f"  {WRITERS} writer + {READERS} reader processes, {DURATION}s each, temp DB:\n  {db_path}")

    summaries = []
    try:
        for mode in modes:
            results, errs = run_mode(mode, db_path)
            summaries.append(summarize(mode, results, errs))
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    # ---- Verdict ----------------------------------------------------------- #
    print("\n" + "=" * 60)
    by = {s["mode"]: s for s in summaries}
    ok = True
    notes = []

    # Spurious-pass guard: every mode must have heard back from ALL workers. If a
    # worker dies at startup, the sums/sets below would be computed over survivors
    # only and 'new' could trivially pass with 0 lock errors. Assert full reporting.
    expected = WRITERS + READERS
    for s in summaries:
        if s["n_results"] != expected:
            ok = False
            notes.append(f"FAIL  mode '{s['mode']}': only {s['n_results']}/{expected} workers reported "
                         f"(partial death) -> results unreliable")
        if s["n_errs"]:
            notes.append(f"WARN  mode '{s['mode']}': {s['n_errs']} worker stderr stream(s) -> inspect output above")

    if "raw" in by:
        raw_locks = by["raw"]["writer_lock_errors"] + by["raw"]["reader_lock_errors"]
        if raw_locks > 0:
            notes.append(f"PASS  control 'raw' genuinely contends: {raw_locks} lock errors with busy_timeout=0")
        else:
            ok = False
            notes.append("FAIL  control 'raw' produced 0 lock errors -> workload didn't overlap; test is inconclusive")

    if "new" in by:
        n = by["new"]
        new_locks = n["writer_lock_errors"] + n["reader_lock_errors"]
        if n["journal_mode"] == ["wal"] and n["busy_timeout"] == [30000]:
            notes.append("PASS  'new' engaged the real fix in every process (journal_mode=wal, busy_timeout=30000)")
        else:
            ok = False
            notes.append(f"FAIL  'new' did NOT engage the fix: journal={n['journal_mode']} busy_timeout={n['busy_timeout']}")
        if new_locks == 0:
            notes.append(f"PASS  'new' (WAL+30s) eliminated lock errors: 0 across {n['writer_commits']} commits + {n['reader_reads']} reads")
        else:
            ok = False
            notes.append(f"FAIL  'new' still hit {new_locks} lock errors")
        if n["other_errors"] == 0:
            notes.append("PASS  'new' had no non-lock errors")
        else:
            ok = False
            notes.append(f"FAIL  'new' had {n['other_errors']} non-lock errors")

    for line in notes:
        print(line)
    print("=" * 60)
    print("VERDICT:", "ALL GOOD" if ok else "PROBLEM")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
