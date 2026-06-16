"""
Verify (a) the DELETE->WAL conversion race the audit found, and (b) that the
HARDENED get_engine() listener degrades gracefully instead of crashing.

PRAGMA journal_mode=WAL does NOT honor busy_timeout: the one-time DELETE->WAL
conversion raises 'database is locked' INSTANTLY if another process holds a write
lock at that moment. Re-asserting WAL on an already-WAL file is a harmless no-op,
so the race is one-time only.

Each scenario: a `holder` subprocess grabs a write lock for HOLD s; a `converter`
connects 0.5 s later. Converter variants:
  raw      : inline busy_timeout=30000 then journal_mode=WAL with NO try/except
             (the pre-hardening listener) -> must FAIL instantly on a DELETE file.
  hardened : the real db.models.get_engine() (try/except around the WAL pragma)
             -> must NOT crash; it proceeds in DELETE mode and a later connect converts.

Scenarios run:
  delete + raw      -> expect LOCK_ERROR ~0.0s   (race is real)
  delete + hardened -> expect ok, journal=delete ~0.0s   (fix swallows the race)
  wal    + hardened -> expect ok, journal=wal ~0.0s      (no-op; race is one-time)

Run: python tests/contention/wal_conversion_race.py
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

HOLD = 4.0
CONVERTER_DELAY = 0.5


def make_delete_db(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE IF NOT EXISTS vehicle_positions "
                "(id INTEGER PRIMARY KEY, vehicle_id TEXT, route_id TEXT, "
                "latitude REAL, longitude REAL, timestamp TEXT)")
    con.commit()
    con.close()


def run_holder(args):
    con = sqlite3.connect(args.db_path, timeout=30)
    while time.time() < args.start:
        time.sleep(0.001)
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT INTO vehicle_positions (vehicle_id, route_id, latitude, longitude, timestamp) "
                "VALUES ('HOLD','10900012',34.7,33.1,'2026-06-16 12:00:00')")
    acquired = time.time()
    time.sleep(HOLD)
    con.execute("COMMIT")
    con.close()
    print(json.dumps({"role": "holder", "held_s": round(time.time() - acquired, 2)}))
    sys.stdout.flush()


def _converter_raw(db_path):
    """Pre-hardening listener sequence, inline, with NO try/except around WAL."""
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA journal_mode=WAL")   # raises instantly under a held write lock
    jm = cur.execute("PRAGMA journal_mode").fetchone()[0]
    cur.close()
    con.close()
    return str(jm).lower()


def _converter_hardened(db_path):
    """The real shipped path."""
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path.replace("\\", "/")
    from db.models import get_engine
    from sqlalchemy import text
    engine = get_engine()
    with engine.connect() as c:
        return str(c.execute(text("PRAGMA journal_mode")).scalar()).lower()


def run_converter(args):
    while time.time() < args.start + CONVERTER_DELAY:
        time.sleep(0.001)
    t0 = time.time()
    res = {"role": "converter", "variant": args.variant}
    try:
        jm = _converter_raw(args.db_path) if args.variant == "raw" else _converter_hardened(args.db_path)
        res["outcome"] = "ok"
        res["journal_mode"] = jm
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        res["outcome"] = "LOCK_ERROR" if ("locked" in msg or "busy" in msg) else "OTHER_ERROR"
        res["error"] = str(e).splitlines()[0][:140]
    res["waited_s"] = round(time.time() - t0, 2)
    print(json.dumps(res))
    sys.stdout.flush()


def run_scenario(initial_mode, variant, db_path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    make_delete_db(db_path)
    if initial_mode == "wal":
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.close()

    start = time.time() + 1.0
    procs = []
    for role, extra in (("holder", []), ("converter", ["--variant", variant])):
        cmd = [sys.executable, os.path.abspath(__file__), "--role", role,
               "--db-path", db_path, "--start", repr(start)] + extra
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))

    rows, errs = [], []
    for p in procs:
        out, err = p.communicate(timeout=HOLD + 60)
        if err.strip():
            errs.append(err.strip())
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    errs.append("unparseable: " + line[:80])
    conv = next((r for r in rows if r["role"] == "converter"), None)
    hold = next((r for r in rows if r["role"] == "holder"), None)
    return conv, hold, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="orchestrator")
    ap.add_argument("--db-path")
    ap.add_argument("--variant", default="hardened")
    ap.add_argument("--start", type=lambda s: float(s))
    args = ap.parse_args()

    if args.role == "holder":
        run_holder(args)
        return
    if args.role == "converter":
        run_converter(args)
        return

    tmpdir = tempfile.mkdtemp(prefix="wal_conv_")
    db_path = os.path.join(tmpdir, "conv.db")
    print("WAL conversion-under-write-lock test")
    print(f"  holder holds a write lock {HOLD}s; converter connects 0.5s in.\n  temp DB: {db_path}\n")

    cases = [("delete", "raw"), ("delete", "hardened"), ("wal", "hardened")]
    out = {}
    try:
        for initial, variant in cases:
            conv, hold, errs = run_scenario(initial, variant, db_path)
            out[(initial, variant)] = conv
            print(f"=== file={initial}  converter={variant} ===")
            if hold:
                print(f"  holder held the lock {hold['held_s']}s")
            if conv:
                line = f"  converter: outcome={conv['outcome']} waited={conv['waited_s']}s"
                if "journal_mode" in conv:
                    line += f" journal_mode={conv['journal_mode']}"
                if "error" in conv:
                    line += f"  ({conv['error']})"
                print(line)
            else:
                print("  converter: NO RESULT (worker died)")
            if errs:
                print(f"  [stderr] {errs[:1]}")
            print()
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

    print("=" * 66)
    raw = out.get(("delete", "raw"))
    hard = out.get(("delete", "hardened"))
    walno = out.get(("wal", "hardened"))
    ok = True

    if raw and raw["outcome"] == "LOCK_ERROR" and raw["waited_s"] < 1.0:
        print(f"CONFIRMED  raw (pre-hardening) DELETE->WAL conversion FAILS INSTANTLY under a "
              f"held write lock ({raw['waited_s']}s) -> the race is real")
    else:
        ok = False
        print(f"UNEXPECTED raw delete-scenario did not fail instantly: {raw}")

    if hard and hard["outcome"] == "ok" and hard.get("journal_mode") == "delete":
        print(f"FIXED      hardened get_engine() does NOT crash on the race: proceeds in "
              f"journal=delete ({hard['waited_s']}s); a later connect will convert")
    else:
        ok = False
        print(f"PROBLEM    hardened delete-scenario unexpected: {hard}")

    if walno and walno["outcome"] == "ok" and walno.get("journal_mode") == "wal" and walno["waited_s"] < 1.0:
        print(f"CONFIRMED  on an already-WAL file the WAL pragma is a no-op even under a held "
              f"lock ({walno['waited_s']}s) -> race is one-time; offline pre-convert removes it")
    else:
        ok = False
        print(f"PROBLEM    wal no-op scenario unexpected: {walno}")

    print("=" * 66)
    print("VERDICT:", "ALL GOOD" if ok else "PROBLEM")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
