"""
WAL checkpoint-starvation check: does bus_data.db-wal stay BOUNDED when a heavy
writer runs alongside a continuous reader (the bot's read pattern)? If the WAL
auto-checkpoint were starved by readers, -wal would grow ~linearly with writes.

Standalone: python tests/contention/wal_checkpoint_check.py
"""
import os
import sys
import tempfile
import threading
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

tmp = tempfile.mkdtemp(prefix="wal_chk_")
db = os.path.join(tmp, "wal.db")
os.environ["DATABASE_URL"] = "sqlite:///" + db.replace("\\", "/")

from db.models import Base, get_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

eng = get_engine()
Base.metadata.create_all(eng)

with eng.connect() as c:
    autockpt = c.execute(text("PRAGMA wal_autocheckpoint")).scalar()
    page_size = c.execute(text("PRAGMA page_size")).scalar()
    jm = c.execute(text("PRAGMA journal_mode")).scalar()
print(f"journal_mode={jm}  wal_autocheckpoint={autockpt} pages  page_size={page_size} B")
print(f"=> auto-checkpoint threshold ~= {autockpt * page_size / 1048576:.2f} MB of WAL")

stop = threading.Event()


def reader():
    # continuous short reads (open/query/close), like the ETA bot
    while not stop.is_set():
        with eng.connect() as c:
            c.execute(text("SELECT COUNT(*) FROM vehicle_positions")).scalar()


t = threading.Thread(target=reader)
t.start()

N = 20000
max_wal = 0
wal_path = db + "-wal"
for i in range(N):
    with eng.begin() as c:
        c.execute(text("INSERT INTO vehicle_positions (vehicle_id, route_id, latitude, longitude, timestamp) "
                       "VALUES ('X', '10900012', 34.7, 33.1, '2026-06-16 12:00:00')"))
    if i % 500 == 0 and os.path.exists(wal_path):
        max_wal = max(max_wal, os.path.getsize(wal_path))

stop.set()
t.join()

final = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
print(f"\ninserted {N} rows with a CONCURRENT continuous reader thread")
print(f"max -wal during run : {max_wal / 1048576:.2f} MB")
print(f"final -wal size     : {final / 1048576:.2f} MB")
print(f"main db size        : {os.path.getsize(db) / 1048576:.2f} MB")
if max_wal < 50 * 1048576:
    print("VERDICT: WAL stayed BOUNDED -> no checkpoint starvation under short-read load")
else:
    print("VERDICT: WAL GREW LARGE -> investigate checkpoint starvation")

eng.dispose()
shutil.rmtree(tmp, ignore_errors=True)
