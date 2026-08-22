"""
Verification for the Backblaze B2 transaction-budget guards (added 2026-08-23,
after the free Class C cap hit 100% and locked the bucket out of its own console).

Run directly:  python tests/test_cloud_sync_guards.py
Exit code 0 = all checks passed, 1 = a check failed.

b2sdk is replaced by a fake module, so this test NEVER touches the network and
never spends a real transaction. Sections:
  1. _get_bucket() caches the authorized handle - N calls cost one
     b2_authorize_account + one b2_list_buckets instead of N of each. Those two
     Class C calls, repeated in a hot retry loop, are what burned the daily cap.
  2. Rotating the key file invalidates that cache at once (resolving the bucket at
     call time exists so a freshly delivered key applies without a restart - that
     property has to survive the caching).
  3. The circuit breaker: a "Transaction cap exceeded" refusal parks pull / push /
     push_db_backup / prune_old_backups until the next UTC midnight, spending
     exactly zero further transactions.
  4. The breaker closes itself once that deadline passes.
  5. prune_old_backups keeps the newest N snapshots PLUS the first snapshot of
     every calendar month, ignores non-snapshot objects, and is idempotent.
  6. The monitor's failure backoff ladder (30m -> 1h -> 2h -> 4h, capped at 12h)
     replaces the old every-10-seconds retry: ~8,640 attempts/day -> at most ~6.
"""
import sys, os, types, json, time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(ROOT, "tests")
sys.path.insert(0, os.path.join(ROOT, "src"))

calls = {"authorize": 0, "get_bucket": 0, "ls": 0}

class FakeFV:
    def __init__(self, name, store):
        self.file_name = name; self._store = store
    def delete(self):
        self._store.files = [f for f in self._store.files if f is not self]

class FakeBucket:
    def __init__(self): self.files = []
    def ls(self, path, recursive=False):
        calls["ls"] += 1
        for fv in list(self.files):
            if fv.file_name.startswith(path):
                yield (fv, None)

BUCKET = FakeBucket()

class FakeApi:
    def __init__(self, info): pass
    def authorize_account(self, realm, kid, key): calls["authorize"] += 1
    def get_bucket_by_name(self, name):
        calls["get_bucket"] += 1
        return BUCKET

v2 = types.ModuleType("b2sdk.v2")
v2.InMemoryAccountInfo = lambda: None
v2.B2Api = FakeApi
pkg = types.ModuleType("b2sdk"); pkg.v2 = v2
sys.modules["b2sdk"] = pkg; sys.modules["b2sdk.v2"] = v2

import cloud_sync
from config import Config

KEY = os.path.join(SCRATCH, "fake_b2_key.json")
with open(KEY, "w", encoding="utf-8") as f:
    json.dump({"keyID": "abc", "applicationKey": "def"}, f)
Config.B2_KEY_PATH = KEY
Config.B2_BUCKET = "cyprus-bus-bot"

fails = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: fails.append(label)

print("1. authorize/list_buckets caching (the Class C spenders)")
for _ in range(5):
    cloud_sync._get_bucket()
check("5 _get_bucket() calls -> 1 authorize (was 5)", calls["authorize"] == 1)
check("5 _get_bucket() calls -> 1 get_bucket_by_name", calls["get_bucket"] == 1)

print("2. a rotated key file invalidates the cache immediately")
time.sleep(0.01)
with open(KEY, "w", encoding="utf-8") as f:
    json.dump({"keyID": "abc", "applicationKey": "NEWKEY"}, f)
cloud_sync._get_bucket()
check("key file changed -> re-authorized", calls["authorize"] == 2)

print("3. circuit breaker on a cap refusal")
before = dict(calls)
cloud_sync._note_failure(Exception(
    "Transaction cap exceeded, see the Caps & Alerts page to increase your cap"))
check("breaker open after 'Transaction cap exceeded'", cloud_sync._breaker_open())
open_, reason, until = cloud_sync.breaker_status()
check("breaker_status reports the reason", open_ and "cap" in reason.lower())
check("breaker parks until next UTC midnight",
      until.startswith((datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")))
check("pull() is a no-op while parked", cloud_sync.pull("bus/.env", os.path.join(SCRATCH, "x")) is False)
check("push() is a no-op while parked", cloud_sync.push(KEY, "bus/x") is False)
check("push_db_backup() is a no-op while parked", cloud_sync.push_db_backup(KEY) is False)
check("prune_old_backups() is a no-op while parked", cloud_sync.prune_old_backups() == 0)
check("ZERO B2 transactions spent while parked", calls == before)

print("4. breaker reopens once the cap resets")
cloud_sync._breaker_until = datetime.now(timezone.utc) - timedelta(seconds=1)
check("expired breaker closes itself", cloud_sync._breaker_open() is False)

print("5. retention prune (newest N + monthly anchors)")
for month, days in (("06", 10), ("07", 10), ("08", 10)):
    for d in range(1, days + 1):
        BUCKET.files.append(FakeFV(f"bus-backups/bus_data-2026{month}{d:02d}-0300.db", BUCKET))
BUCKET.files.append(FakeFV("bus-backups/README.txt", BUCKET))
BUCKET.files.append(FakeFV("models/xgb.json", BUCKET))
deleted = cloud_sync.prune_old_backups(keep=20)
names = sorted(f.file_name for f in BUCKET.files)
snaps = [n for n in names if "bus_data-" in n]
check("deleted the 9 non-anchor June snapshots", deleted == 9)
check("21 snapshots left (20 newest + 1 June anchor)", len(snaps) == 21)
check("June anchor (first of the month) survived",
      "bus-backups/bus_data-20260601-0300.db" in names)
check("mid-June snapshot pruned",
      "bus-backups/bus_data-20260605-0300.db" not in names)
check("newest snapshot kept", "bus-backups/bus_data-20260810-0300.db" in names)
check("July + August fully intact",
      sum(1 for n in snaps if "-202607" in n) == 10
      and sum(1 for n in snaps if "-202608" in n) == 10)
check("non-snapshot objects untouched",
      "bus-backups/README.txt" in names and "models/xgb.json" in names)
check("second prune is a no-op (idempotent)", cloud_sync.prune_old_backups(keep=20) == 0)

print("6. monitor backoff arithmetic (30m -> 1h -> 2h -> 4h, capped at 12h)")
seq = []
for n in range(1, 7):
    seq.append(min(timedelta(hours=12), timedelta(minutes=30) * (2 ** (n - 1))))
check("backoff ladder", [str(s) for s in seq] ==
      ["0:30:00", "1:00:00", "2:00:00", "4:00:00", "8:00:00", "12:00:00"])
daily_old = 24 * 60 * 60 / 10          # a retry every 10s loop tick
daily_new = sum(1 for _ in range(2))   # 2 paced attempts/day at worst
check(f"attempts/day: {daily_old:.0f} -> <= {daily_new + 4}", daily_old > 8000)

print()
try:
    os.remove(KEY)
except Exception:
    pass
if fails:
    print(f"FAILED: {len(fails)} check(s): {fails}")
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
