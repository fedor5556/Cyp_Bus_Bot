"""
cloud_sync.py - durable, fail-safe cloud file-transfer layer (Backblaze B2).

A bidirectional large-file channel (pull / push / push_db_backup) for the Cyprus
Bus tracker, so secrets and large files can move between the developer and the
friend-hosted server WITHOUT going through public git or the send-only Admin Hub.
See remote_host_architecture_guideLAST.md section 13.5 and CLAUDE_HISTORY.md section 4.
(Originally designed against Google Cloud Storage; ported to Backblaze B2 because
B2's free tier needs no payment card. The public interface is provider-agnostic.)

Design rules baked in (from the project's hard-won lessons):
  * Fail-safe: every public function catches all exceptions, returns a value, and
    NEVER raises into a caller. A cloud outage must never crash the monitor loop.
  * Lazy import: b2sdk is imported INSIDE the functions, so this module imports
    cleanly even when the SDK or the key is absent. The whole layer is a silent
    no-op until armed (graceful degradation).
  * Atomic writes: downloads land in a "<dest>.part" sidecar, then os.replace()
    into place, so a torn download never leaves a half-written destination.
  * SQLite online-backup: DB snapshots use the stdlib sqlite3 backup API, not a raw
    file copy, so a live WAL is never captured mid-write.

The application key is the only secret; it arrives out-of-band via Telegram (DM a
small .json to the bus bot, or the /armb2 command) and is stored gitignored at
Config.B2_KEY_PATH as {"keyID": ..., "applicationKey": ...}. The bucket NAME is
not a secret (the bucket itself is private and key-scoped).
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from config import Config

# ---------------------------------------------------------------------------
# Transaction-budget guards (added 2026-08-23 after the free Backblaze Class C
# cap was burned to 100% and locked even the web console out of the bucket).
#
# B2's free tier allows 2,500 Class C transactions/day. Every _get_bucket() costs
# TWO of them (b2_authorize_account + b2_list_buckets), and a *failing* call costs
# exactly as much as a working one -- so any hot retry loop against a broken B2
# both blows the cap and then keeps it blown. Two defences live here:
#   1. _get_bucket() caches the authorized bucket handle, so a burst of calls
#      (or the 10-minute .env sync) re-authorizes at most once per _AUTH_TTL.
#      The cache is keyed on the key file's fingerprint, so a freshly delivered
#      key still takes effect immediately -- that was the point of resolving the
#      bucket at call time, and it is preserved.
#   2. A circuit breaker: once B2 answers "cap exceeded" or "unauthorized", every
#      public function here becomes a no-op until the cap resets (next UTC
#      midnight) or, for auth errors, for an hour. Spending transactions to be
#      told you have no transactions left is the whole failure mode.
# ---------------------------------------------------------------------------
_AUTH_TTL = timedelta(minutes=20)
_cached_bucket = None
_cached_bucket_at = None
_cached_bucket_key = None

_breaker_until = None
_breaker_reason = ""


def _breaker_open():
    """True while the layer is deliberately parked after a cap/auth refusal."""
    global _breaker_until
    if _breaker_until is None:
        return False
    if datetime.now(timezone.utc) >= _breaker_until:
        _breaker_until = None
        return False
    return True


def _note_failure(exc):
    """Inspect a B2 failure and, if it is one that MORE requests cannot fix, park
    the layer instead of hammering it. Never raises."""
    global _breaker_until, _breaker_reason, _cached_bucket
    try:
        _cached_bucket = None  # a failed call may mean a stale/broken auth
        msg = str(exc).lower()
        now = datetime.now(timezone.utc)
        if "storage cap" in msg or "storage_cap" in msg:
            # A FULL bucket is fixed by DELETING, not by waiting, and deletes are
            # Class A (free). Parking here would deadlock: no upload -> no prune ->
            # never any room. Leave the breaker closed so the caller can prune.
            print("[cloud_sync] B2 storage cap exceeded - the bucket is full. "
                  "Retention prune is the fix here, not a retry.")
            return
        if "cap exceeded" in msg or "transaction cap" in msg:
            # B2 caps are daily and reset at UTC midnight.
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0)
            _breaker_until, _breaker_reason = tomorrow, "B2 free cap exceeded"
        elif ("unauthorized" in msg or "bad_auth_token" in msg
                or ("invalid" in msg and "key" in msg)):
            _breaker_until, _breaker_reason = now + timedelta(hours=1), "B2 key rejected"
        else:
            return
        print(f"[cloud_sync] {_breaker_reason}: pausing all B2 calls until "
              f"{_breaker_until.isoformat()} UTC.")
    except Exception:
        pass


def breaker_status():
    """(is_open, reason, until_utc_iso) -- for /status style reporting."""
    if not _breaker_open():
        return (False, "", "")
    return (True, _breaker_reason, _breaker_until.isoformat())


def is_configured():
    """Master gate. True only when a bucket name is set AND the key file exists.
    Every other public function short-circuits to a no-op when this is False, so
    the bus tracker runs exactly as before until the layer is armed."""
    try:
        if not Config.B2_BUCKET:
            return False
        return bool(Config.B2_KEY_PATH) and os.path.exists(Config.B2_KEY_PATH)
    except Exception:
        return False


def _load_key():
    """Read the stored application key, tolerating the field spellings B2 and
    common tools use. Raises on a malformed file -- callers wrap in try/except."""
    with open(Config.B2_KEY_PATH, "r", encoding="utf-8") as f:
        creds = json.load(f)
    key_id = creds.get("keyID") or creds.get("applicationKeyId") or creds.get("key_id")
    app_key = creds.get("applicationKey") or creds.get("application_key") or creds.get("appKey")
    if not key_id or not app_key:
        raise ValueError("key file missing keyID/applicationKey fields")
    return key_id, app_key


def _key_fingerprint():
    """(mtime, size) of the key file -- cheap change detection so a newly
    delivered/rotated key invalidates the cached authorization at once."""
    try:
        st = os.stat(Config.B2_KEY_PATH)
        return (st.st_mtime, st.st_size, Config.B2_BUCKET)
    except Exception:
        return None


def _get_bucket(force_fresh=False):
    """Authorize against B2 and resolve the bucket at CALL time (not import time),
    so the monitor picks up a key the bot stored after the monitor had already
    started. Raises if the SDK/key is unusable -- callers wrap this in try/except.

    The handle is cached for _AUTH_TTL because authorize + get_bucket_by_name are
    two billable Class C transactions on a 2,500/day free budget; re-doing them for
    every 10-minute .env pull is pure waste. The cache is dropped as soon as the key
    file changes (rotation) or any call fails (see _note_failure)."""
    global _cached_bucket, _cached_bucket_at, _cached_bucket_key
    fingerprint = _key_fingerprint()
    if (not force_fresh and _cached_bucket is not None
            and _cached_bucket_key == fingerprint
            and _cached_bucket_at is not None
            and datetime.now(timezone.utc) - _cached_bucket_at < _AUTH_TTL):
        return _cached_bucket

    from b2sdk.v2 import InMemoryAccountInfo, B2Api
    key_id, app_key = _load_key()
    api = B2Api(InMemoryAccountInfo())
    api.authorize_account("production", key_id, app_key)
    bucket = api.get_bucket_by_name(Config.B2_BUCKET)
    _cached_bucket, _cached_bucket_at, _cached_bucket_key = (
        bucket, datetime.now(timezone.utc), fingerprint)
    return bucket


def pull(object_name, dest_path, *, validate_contains=None, no_clobber=False):
    """Download `object_name` from the bucket to `dest_path`, atomically.

    validate_contains: if set (str), the downloaded body must contain this marker
        or the download is rejected and discarded (returns False). Guards against
        deploying a truncated/0-byte/wrong file.
    no_clobber: if True and `dest_path` already exists, short-circuit to True
        without downloading. This makes a recurring pull self-disabling -- the
        file's existence is the sentinel, so it only ever writes once.

    Returns True on success (or a no-clobber skip), False on any failure.
    """
    if not is_configured() or _breaker_open():
        return False

    part_path = dest_path + ".part"
    try:
        if no_clobber and os.path.exists(dest_path):
            return True

        bucket = _get_bucket()
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        downloaded = bucket.download_file_by_name(object_name)
        downloaded.save_to(part_path)

        if validate_contains is not None:
            with open(part_path, "rb") as f:
                body = f.read()
            marker = validate_contains.encode("utf-8") if isinstance(validate_contains, str) else validate_contains
            if marker not in body:
                os.remove(part_path)
                print(f"[cloud_sync] pull rejected: marker not found in {object_name}.")
                return False

        os.replace(part_path, dest_path)  # atomic
        print(f"[cloud_sync] pull ok: {object_name} -> {dest_path}")
        return True
    except Exception as e:
        print(f"[cloud_sync] pull failed for {object_name}: {e}")
        _note_failure(e)
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        return False


def push(local_path, object_name):
    """Upload a local file to the bucket as `object_name`. Returns True/False."""
    if not is_configured() or _breaker_open():
        return False
    try:
        if not os.path.exists(local_path):
            print(f"[cloud_sync] push failed: {local_path} does not exist.")
            return False
        bucket = _get_bucket()
        bucket.upload_local_file(local_file=local_path, file_name=object_name)
        print(f"[cloud_sync] push ok: {local_path} -> {object_name}")
        return True
    except Exception as e:
        print(f"[cloud_sync] push failed for {local_path}: {e}")
        _note_failure(e)
        return False


def push_db_backup(db_path, prefix="bus-backups"):
    """Snapshot the live SQLite DB via the online-backup API and upload it as
    `<prefix>/bus_data-<YYYYMMDD-HHMM>.db`. The online backup yields a consistent
    snapshot without capturing a torn WAL; the local snapshot is deleted after
    upload (it is gitignored as data/*.bak in the meantime).

    Returns the uploaded object name (a truthy str) on success, False otherwise.
    """
    if not is_configured() or _breaker_open():
        return False

    snapshot_path = None
    try:
        if not os.path.exists(db_path):
            print(f"[cloud_sync] push_db_backup failed: {db_path} does not exist.")
            return False

        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        snapshot_path = f"{db_path}.{timestamp}.bak"

        # SQLite online backup: a consistent snapshot of a live DB (no torn WAL).
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(snapshot_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        object_name = f"{prefix}/bus_data-{timestamp}.db"
        return object_name if push(snapshot_path, object_name) else False
    except Exception as e:
        print(f"[cloud_sync] push_db_backup failed: {e}")
        _note_failure(e)
        return False
    finally:
        try:
            if snapshot_path and os.path.exists(snapshot_path):
                os.remove(snapshot_path)
        except Exception:
            pass


def prune_old_backups(keep=20, prefix="bus-backups"):
    """Delete old DB snapshots under `prefix/`, keeping the newest `keep` plus the
    first snapshot of every calendar month (a permanent long-tail anchor).

    This is the client-side stand-in for the B2 lifecycle rule that was never
    applied by hand (open since 2026-06-11). Without it the bucket grows by two
    snapshots a day -- ~90 MB each by August 2026 -- and walks straight into the
    10 GB free-tier storage cap, at which point every upload starts failing and
    the caller's retry loop burns the daily transaction budget.

    Cost: one b2_list_file_names (Class C) per 1,000 objects; the deletes
    themselves are Class A (free). Runs at most twice a day from the monitor.

    Snapshot names are `bus_data-YYYYMMDD-HHMM.db`, so lexical sort == chronological
    sort. Anything under the prefix that does NOT match that pattern is left alone.
    Returns the number of objects deleted (0 on any failure -- never raises).
    """
    if not is_configured() or _breaker_open():
        return 0
    try:
        keep = max(1, int(keep))
    except Exception:
        keep = 20

    monthly_anchors = {}
    try:
        bucket = _get_bucket()
        versions = []
        for entry in bucket.ls(f"{prefix}/", recursive=True):
            file_version = entry[0] if isinstance(entry, tuple) else entry
            name = getattr(file_version, "file_name", "")
            base = name.rsplit("/", 1)[-1]
            if base.startswith("bus_data-") and base.endswith(".db"):
                versions.append((base, file_version))

        if len(versions) <= keep:
            return 0

        versions.sort(key=lambda pair: pair[0])          # oldest first

        # Keep the newest `keep` snapshots PLUS the first snapshot of every calendar
        # month, forever. A pure sliding window would mean that if the live DB ever
        # got corrupted and nobody noticed for two weeks, every surviving backup
        # would already be post-corruption. The monthly anchors are ~1 per month,
        # so they cost almost nothing against the 10 GB tier but keep a long tail.
        survivors = {id(fv) for _, fv in versions[len(versions) - keep:]}
        for base, file_version in versions:              # oldest first == first of month
            month = base[len("bus_data-"):len("bus_data-") + 6]   # YYYYMM
            if month and month not in monthly_anchors:
                monthly_anchors[month] = base
                survivors.add(id(file_version))

        doomed = [(base, fv) for base, fv in versions if id(fv) not in survivors]
        deleted = 0
        for base, file_version in doomed:
            try:
                file_version.delete()
                deleted += 1
            except Exception as del_err:
                print(f"[cloud_sync] prune: could not delete {base}: {del_err}")
                _note_failure(del_err)
                break                                     # stop on the first refusal
        if deleted:
            print(f"[cloud_sync] prune ok: deleted {deleted} old backup(s); kept the "
                  f"newest {keep} + {len(monthly_anchors)} monthly anchor(s).")
        return deleted
    except Exception as e:
        print(f"[cloud_sync] prune_old_backups failed: {e}")
        _note_failure(e)
        return 0
