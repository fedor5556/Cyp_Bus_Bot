"""
Targeted cross-process test for finding #1's EXACT claim: a write transaction held
longer than the busy timeout makes a second writer raise SQLITE_BUSY
("database is locked"). This is the scenario the 5 s default (old) hits and the
30 s fix (new) survives.

Two REAL processes per mode:
  long      : grabs the write lock (BEGIN IMMEDIATE + INSERT) and HOLDS it HOLD s.
  contender : 0.5 s later tries to commit one write. It must wait for `long`.

Expectation:
  old (busy_timeout=5000): contender waits ~5 s then RAISES 'database is locked'.
  new (WAL, busy_timeout=30000): contender waits ~HOLD s then COMMITS successfully.

HOLD = 8 s sits deliberately between the two timeouts.

Run:  python tests/contention/longlock_test.py
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
sys.path.insert(0, HERE)

from sqlalchemy import text  # noqa: E402
from contention_test import make_engine, build_schema  # noqa: E402

HOLD = 8.0            # seconds the long writer holds the lock (between 5 s and 30 s)
CONTENDER_DELAY = 0.5  # contender starts this long after the synced start


def run_long(args):
    engine = make_engine(args.mode, args.db_url)
    raw = engine.raw_connection()
    cur = raw.cursor()
    while time.time() < args.start:
        time.sleep(0.001)
    cur.execute("BEGIN IMMEDIATE")
    cur.execute("INSERT INTO vehicle_positions (vehicle_id, route_id, latitude, longitude, timestamp) "
                "VALUES ('LONG', '10900012', 34.7, 33.1, '2026-06-16 12:00:00')")
    acquired = time.time()
    time.sleep(HOLD)
    cur.execute("COMMIT")
    released = time.time()
    raw.close()
    print(json.dumps({"role": "long", "mode": args.mode, "held_s": round(released - acquired, 2)}))
    sys.stdout.flush()


def run_contender(args):
    engine = make_engine(args.mode, args.db_url)
    # Probe pragmas first (own connection) as evidence of which config is active.
    with engine.connect() as c:
        jm = str(c.execute(text("PRAGMA journal_mode")).scalar()).lower()
        bt = int(c.execute(text("PRAGMA busy_timeout")).scalar())

    while time.time() < args.start + CONTENDER_DELAY:
        time.sleep(0.001)

    res = {"role": "contender", "mode": args.mode, "journal_mode": jm, "busy_timeout": bt}
    t0 = time.time()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO vehicle_positions (vehicle_id, route_id, latitude, longitude, timestamp) "
                     "VALUES ('CONT', '10900012', 34.7, 33.1, :ts)"),
                {"ts": datetime(2026, 6, 16, 12, 0, 0)})
        res["outcome"] = "committed"
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        res["outcome"] = "LOCK_ERROR" if ("locked" in msg or "busy" in msg) else "OTHER_ERROR"
        res["error"] = str(e).splitlines()[0][:140]
    res["waited_s"] = round(time.time() - t0, 2)
    print(json.dumps(res))
    sys.stdout.flush()


def run_mode(mode, db_path):
    db_url = "sqlite:///" + db_path.replace("\\", "/")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    build_schema(db_url)

    start = time.time() + 1.0
    procs = []
    for role in ("long", "contender"):
        cmd = [sys.executable, os.path.abspath(__file__), "--role", role,
               "--mode", mode, "--db-url", db_url, "--start", repr(start)]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))

    results, errs = [], []
    for p in procs:
        out, err = p.communicate(timeout=HOLD + 60)
        if err.strip():
            errs.append(err.strip())
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    errs.append("unparseable: " + line[:80])
    return results, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="orchestrator")
    ap.add_argument("--mode")
    ap.add_argument("--db-url", dest="db_url")
    ap.add_argument("--start", type=lambda s: float(s))
    args = ap.parse_args()

    if args.role == "long":
        run_long(args)
        return
    if args.role == "contender":
        run_contender(args)
        return

    tmpdir = tempfile.mkdtemp(prefix="bus_longlock_")
    db_path = os.path.join(tmpdir, "longlock.db")
    print("Long-held-lock cross-process test")
    print(f"  long writer holds the write lock {HOLD}s; a 2nd process tries to write 0.5s in.\n  temp DB: {db_path}")

    rows = {}
    try:
        for mode in ("old", "new"):
            results, errs = run_mode(mode, db_path)
            cont = next((r for r in results if r["role"] == "contender"), None)
            longw = next((r for r in results if r["role"] == "long"), None)
            rows[mode] = (cont, longw, errs)
            print(f"\n=== mode: {mode} ===")
            if longw:
                print(f"  long writer held the lock for {longw['held_s']} s")
            if cont:
                print(f"  contender: journal={cont['journal_mode']} busy_timeout={cont['busy_timeout']} "
                      f"-> outcome={cont['outcome']} after waiting {cont['waited_s']} s")
                if "error" in cont:
                    print(f"             error: {cont['error']}")
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

    print("\n" + "=" * 64)
    ok = True
    old_c, old_long = rows.get("old", (None, None, None))[:2]
    new_c, new_long = rows.get("new", (None, None, None))[:2]

    # A run is only meaningful if the long writer actually grabbed and held the lock.
    # Guard against a flaky run where 'long' never reported (no contention -> the
    # contender would commit in ~0s and falsely "pass").
    min_wait = HOLD - CONTENDER_DELAY - 1.5   # contender should wait ~ this long for the holder

    def held_ok(longw):
        return longw is not None and longw.get("held_s", 0) >= HOLD - 1.0

    if not held_ok(new_long):
        ok = False
        print(f"INCONCLUSIVE  'new' run had no genuine lock hold (long={new_long}); "
              f"cannot trust the result -> rerun")
    if old_c and old_c["outcome"] == "LOCK_ERROR":
        print(f"PASS  pre-fix (busy_timeout=5000) RAISES SQLITE_BUSY at ~{old_c['waited_s']}s "
              f"-> finding #1's exact failure reproduced")
    else:
        ok = False
        print(f"NOTE  pre-fix did NOT raise (got {old_c['outcome'] if old_c else None}); "
              f"machine may be faster/slower than the 5 s window")

    if (new_c and new_c["outcome"] == "committed" and new_c["journal_mode"] == "wal"
            and new_c["busy_timeout"] == 30000 and new_c["waited_s"] >= min_wait and held_ok(new_long)):
        print(f"PASS  the fix (WAL, busy_timeout=30000) genuinely WAITS {new_c['waited_s']}s "
              f"(>= {min_wait:.1f}s) and COMMITS -> no SQLITE_BUSY where the old path failed")
    else:
        ok = False
        print(f"FAIL  the fix did not behave as expected (or contender didn't truly wait): "
              f"cont={new_c} long={new_long}")

    print("=" * 64)
    print("VERDICT:", "ALL GOOD" if ok else "PROBLEM")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
