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
from datetime import datetime

from config import Config


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


def _get_bucket():
    """Authorize against B2 and resolve the bucket at CALL time (not import time),
    so the monitor picks up a key the bot stored after the monitor had already
    started. Raises if the SDK/key is unusable -- callers wrap this in try/except."""
    from b2sdk.v2 import InMemoryAccountInfo, B2Api
    key_id, app_key = _load_key()
    api = B2Api(InMemoryAccountInfo())
    api.authorize_account("production", key_id, app_key)
    return api.get_bucket_by_name(Config.B2_BUCKET)


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
    if not is_configured():
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
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        return False


def push(local_path, object_name):
    """Upload a local file to the bucket as `object_name`. Returns True/False."""
    if not is_configured():
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
        return False


def push_db_backup(db_path, prefix="bus-backups"):
    """Snapshot the live SQLite DB via the online-backup API and upload it as
    `<prefix>/bus_data-<YYYYMMDD-HHMM>.db`. The online backup yields a consistent
    snapshot without capturing a torn WAL; the local snapshot is deleted after
    upload (it is gitignored as data/*.bak in the meantime).

    Returns the uploaded object name (a truthy str) on success, False otherwise.
    """
    if not is_configured():
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
        return False
    finally:
        try:
            if snapshot_path and os.path.exists(snapshot_path):
                os.remove(snapshot_path)
        except Exception:
            pass
