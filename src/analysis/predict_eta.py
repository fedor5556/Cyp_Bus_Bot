import os
import re
import sys
import json
import shutil
import asyncio
import hashlib
import subprocess
from datetime import datetime

# Add parent directory to path so we can import config and models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.models import get_session
from analysis import eta
from config import Config
import cloud_sync


def format_prediction(p):
    """Render one eta.VehiclePrediction into the bot's exact text lines.

    Pure templating over the numbers already computed in `p` - no arithmetic and
    no branching on raw inputs - so the bot's Telegram output can never drift from
    what eta.compute_vehicle_prediction (and therefore the PredictionLog) recorded.
    """
    lines = [f"Vehicle {p.vehicle_id} ({p.direction}){p.status_tag}"]

    if p.status == "at":
        lines.append("  Status: AT PYRGOS CHURCH")
        lines.append("-" * 50)
        return lines

    dist_line = (f"  Distance to Pyrgos Church: {p.smooth_distance_m/1000:.1f} km "
                 f"(Speed: {p.speed_kmh:.1f} km/h){p.dist_tag}")

    if p.status == "passed":
        lines.append(dist_line)
        lines.append("  Status: Passed Pyrgos on current trip (At or heading to terminal)")
        if p.moving:
            lines.append(f"  --> Next ETA to Pyrgos: ~{p.eta_minutes:.1f} minutes (Movement-based)")
        else:
            lines.append(f"  --> Next ETA to Pyrgos: ~{p.eta_minutes:.1f} minutes (Assuming 30km/h once moving)")
        lines.append("-" * 50)
        return lines

    # scheduled / no_schedule both lead with the distance line
    lines.append(dist_line)

    if p.status == "scheduled":
        lines.append(f"  Timetable Schedule: {p.target_scheduled.strftime('%H:%M:%S')}")
        lines.append(f"  Current Status: {p.status_str}")
        if p.moving:
            lines.append(f"  --> EXPECTED ARRIVAL: {p.predicted_arrival.strftime('%H:%M:%S')} (in ~{p.eta_minutes:.1f} minutes)")
        else:
            lines.append(f"  --> EXPECTED ARRIVAL: {p.predicted_arrival.strftime('%H:%M:%S')} (in ~{p.eta_minutes:.1f} minutes) [Stationary]")
    else:  # no_schedule
        if p.moving:
            lines.append(f"  --> Predicted ETA: ~{p.eta_minutes:.1f} minutes (No schedule data)")
        else:
            lines.append("  --> Status: Unknown (No schedule, not moving)")

    lines.append("-" * 50)
    return lines


def get_prediction_text(now=None, session=None):
    """Build the bot's live-ETA text. The per-vehicle math now lives in analysis.eta;
    this function only fetches the latest data, computes each prediction, and formats
    it. `now` and `session` are injectable for deterministic, offline tests (a provided
    session also suppresses the live reactive fetch)."""
    own_session = session is None

    # Perform a reactive check to ensure we have the absolute latest data before
    # querying the DB. Only on the live path - a caller-provided session is a test.
    if own_session:
        try:
            from ingestion.fetch_rt import fetch_realtime_data
            fetch_realtime_data(quiet=True)
        except Exception as e:
            print(f"Reactive fetch failed: {e}")

    if now is None:
        now = datetime.now()

    output = []
    output.append("==================================================")
    output.append("LIVE ETA PREDICTION: PYRGOS CHURCH")
    output.append("==================================================")
    output.append("")

    if own_session:
        session = get_session()

    positions = eta.active_route90_positions(session, now=now)

    if not positions:
        output.append("No active vehicles on Route 90 found.")
        output.append("-" * 50)

        from analysis.schedule import get_next_scheduled_arrival
        next_lemesos = get_next_scheduled_arrival("10900011", eta.TARGET_STOPS["10900011"])
        next_sanida = get_next_scheduled_arrival("10900012", eta.TARGET_STOPS["10900012"])

        if next_lemesos:
            output.append(f"Next Scheduled (Towards Lemesos): {next_lemesos.strftime('%Y-%m-%d %H:%M')}")
        if next_sanida:
            output.append(f"Next Scheduled (Towards Sanida):  {next_sanida.strftime('%Y-%m-%d %H:%M')}")

        if own_session:
            session.close()
        return "\n".join(output)

    for pos in positions:
        try:
            pred = eta.compute_vehicle_prediction(session, pos, now=now)
            if pred is None:
                continue
            output.extend(format_prediction(pred))
        except Exception as e:
            # One vehicle's compute/format must never blank the whole public reply
            # (e.g. a GTFS-integrity gap raising mid-loop). Skip it, keep the rest.
            print(f"Prediction failed for vehicle {getattr(pos, 'vehicle_id', '?')} (skipping): {e}")

    if own_session:
        session.close()
    return "\n".join(output)

def send_telegram_alert(message):
    """Sends a message via Telegram API for alerts/crashes."""
    import requests
    from dotenv import load_dotenv
    load_dotenv()
    
    # You can set CRASH_BOT_TOKEN specifically, or it will fall back to the main token
    token = os.environ.get("CRASH_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            print(f"Failed to send Telegram alert: {e}")
    else:
        print("Note: To receive Telegram alerts, ensure TELEGRAM_CHAT_ID and a BOT_TOKEN are set in .env")

def predict_live_eta():
    print(get_prediction_text())

async def bot_handler(update, context):
    user = update.message.from_user
    allowed_users_env = os.environ.get("ALLOWED_TELEGRAM_USERS")
    
    if allowed_users_env:
        allowed_users = [u.strip().lstrip("@") for u in allowed_users_env.split(",")]
        if user.username not in allowed_users:
            print(f"Unauthorized access attempt from user: {user.username}")
            return

    text = get_prediction_text()
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")

# --- Admin-gated inbound file channel + cloud transfer commands -------------------
# This ETA bot is PUBLIC by design, so these handlers gate on the numeric Telegram
# user ID (Config.ADMIN_IDS, unforgeable). They restore the inbound-file capability
# the send-only Admin Hub lacks WITHOUT touching the Hub. Two inbound paths:
#   1. DM a .env document (caption or filename names the target project) -> written
#      to that sibling project's folder. Telegram's ~20 MB inbound cap is plenty.
#   2. DM a .json document (or /armb2) carrying the B2 application key -> arms the
#      cloud layer in cloud_sync.py for large files (DB backups, ML models).

# Holds the single-instance lock socket for the process lifetime (see start_bot).
_SINGLE_INSTANCE_LOCK = None

# Friendly names for .env routing. Any exact sibling folder name also works, so
# new projects need no code change; aliases are just convenience. "bus" resolves
# at runtime because the project folder is named differently on dev vs server.
ENV_TARGET_ALIASES = {
    "transcriber": "Constan_transcriber_telegram_bot",
}

# B2 objects that monitor.py's 10-minute env sync PULLS (cloud -> local, cloud
# canonical). Mirror of env_sources in monitor.start_monitoring - keep in sync.
# For these, a failed push after a DM delivery means the stale cloud copy will
# REVERT the local file, so the reply must scream about it.
PULLED_ENV_OBJECTS = {"bus/.env", "transcriber/.env"}


def _b2_env_object(folder_name):
    """The B2 object name for a project's .env ('transcriber/.env' etc.) -
    the reverse of ENV_TARGET_ALIASES, so the DM delivery pushes to the same
    object monitor.py pulls from. Unknown projects get '<folder>/.env', which
    the sync never pulls - the push is then just a cloud backup."""
    if folder_name == os.path.basename(Config.BASE_DIR):
        return "bus/.env"
    for alias, folder in ENV_TARGET_ALIASES.items():
        if folder == folder_name:
            return alias + "/.env"
    return folder_name + "/.env"


def _resolve_env_target(target):
    """Alias/folder -> sibling folder name, or None if the name is unsafe.
    Shared by the .env delivery and /envrestore so they can never disagree."""
    folder_name = ENV_TARGET_ALIASES.get(target.lower(), target)
    if target.lower() == "bus":
        folder_name = os.path.basename(Config.BASE_DIR)
    # Strict charset = no path traversal (no slashes, dots, drive letters).
    if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_name):
        return None
    return folder_name


def _missing_required_keys(folder_name, env_text):
    """Required key names for this project per Admin_hub/projects.json
    ('env_keys') that the delivered file does NOT define (or defines empty).
    Catches a wrong/truncated .env at delivery time, before a restart breaks
    the bot. Best-effort: no projects.json, no entry, no env_keys -> []."""
    try:
        hub_projects = os.path.join(os.path.dirname(Config.BASE_DIR),
                                    "Admin_hub", "projects.json")
        with open(hub_projects, "r", encoding="utf-8-sig") as f:
            entries = json.load(f)
        required = []
        for spec in entries.values():
            path = (spec.get("path") or "").replace("\\", "/").rstrip("/")
            if path.rsplit("/", 1)[-1] == folder_name:
                required = spec.get("env_keys", [])
                break
        if not required:
            return []
        have = set()
        for line in env_text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                if val.strip():
                    have.add(key.strip())
        return [k for k in required if k not in have]
    except Exception:
        return []


def _cloud_env_note(dest, obj):
    """Push a project's .env to B2 and describe the outcome for the reply."""
    if not cloud_sync.is_configured():
        return "☁️ Cloud layer not armed - local file only (no sync conflict possible)."
    if cloud_sync.push(dest, obj):
        return f"☁️ Cloud copy '{obj}' updated too - the 10-min env sync stays in step."
    if obj in PULLED_ENV_OBJECTS:
        return (f"🚨 FAILED to update the cloud copy '{obj}'. If a different copy exists in "
                f"the bucket, the 10-min env sync will OVERWRITE the local file! Fix B2 (or "
                f"re-upload '{obj}' in the B2 console) and resend, then verify the "
                f"fingerprint via the Hub's /env_check.")
    return f"⚠️ Cloud backup push of '{obj}' failed (harmless: nothing pulls it back)."


def _is_admin(update):
    """Numeric-ID admin gate. Fails closed when Config.ADMIN_IDS is empty (no env)."""
    user = update.effective_user
    return bool(user) and user.id in Config.ADMIN_IDS


def _store_b2_key(key_id, app_key):
    """Atomically persist the B2 application key where cloud_sync expects it."""
    key_path = Config.B2_KEY_PATH
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    part_path = key_path + ".part"
    with open(part_path, "w", encoding="utf-8") as f:
        json.dump({"keyID": key_id, "applicationKey": app_key}, f)
    os.replace(part_path, key_path)


async def document_upload_handler(update, context):
    """Route admin document uploads: .env -> project env delivery, .json -> B2 key.
    Non-admins get silence (public bot)."""
    if not _is_admin(update):
        return
    doc = update.message.document if update.message else None
    if doc is None:
        return

    name = (doc.file_name or "").lower()
    if name.endswith(".env"):
        await _env_upload(update, context, doc)
    elif name.endswith(".json"):
        await _b2_key_upload(update, context, doc)
    else:
        await update.message.reply_text(
            "Unsupported file. Send a project .env (caption = project folder or alias) "
            "or a B2 application key as .json."
        )


async def _env_upload(update, context, doc):
    """Write a DM'd .env into a sibling project's folder. The target comes from the
    message caption, or from the filename stem (e.g. transcriber.env). The previous
    .env (if any) is kept as .env.bak. The file only takes effect when that project
    is restarted via the Admin Hub."""
    msg = update.message
    caption = (msg.caption or "").strip()
    fname = doc.file_name or ""
    stem = fname[:-4].strip() if fname.lower().endswith(".env") else ""
    target = caption or stem
    if not target:
        await msg.reply_text(
            "Which project is this .env for? Add the project folder name (or an alias "
            "like 'transcriber') as the file caption, or name the file e.g. transcriber.env."
        )
        return

    folder_name = _resolve_env_target(target)
    if folder_name is None:
        await msg.reply_text(f"Rejected: '{target}' is not a valid project folder name.")
        return

    projects_root = os.path.dirname(Config.BASE_DIR)
    target_dir = os.path.join(projects_root, folder_name)
    if not os.path.isdir(target_dir):
        await msg.reply_text(f"Rejected: no sibling project folder named '{folder_name}'.")
        return

    # .env files are tiny; 64 KB is already generous.
    if doc.file_size and doc.file_size > 64 * 1024:
        await msg.reply_text("Rejected: file too large to be a .env (>64 KB).")
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await msg.reply_text(f"Download failed: {e}")
        return

    # Sanity: non-empty, decodable, and contains at least one KEY=value line
    # (guards against the historical 0-byte .env incident).
    try:
        text = data.decode("utf-8-sig")
    except Exception:
        await msg.reply_text("Rejected: file is not readable text.")
        return
    if not re.search(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", text, re.MULTILINE):
        await msg.reply_text("Rejected: no KEY=value lines found - that does not look like a .env.")
        return

    dest = os.path.join(target_dir, ".env")
    backed_up = False
    try:
        if os.path.exists(dest):
            shutil.copy2(dest, dest + ".bak")
            backed_up = True
        part_path = dest + ".part"
        with open(part_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())  # survive a power cut right after delivery
        os.replace(part_path, dest)
    except Exception as e:
        await msg.reply_text(f"Failed to write .env: {e}")
        return

    # Verification facts: key NAMES and a content fingerprint, never values.
    # The Hub's /env_check shows the same sha256[:10] of the on-disk file, so
    # "what I sent" and "what the project will read at next start" can be
    # compared at a glance instead of testing by hand.
    fingerprint = hashlib.sha256(data).hexdigest()[:10]
    key_names = sorted(set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", text, re.MULTILINE)))

    # Keep the cloud copy in step with the local file. monitor.py's env sync
    # treats B2 as canonical for PULLED_ENV_OBJECTS: before this push existed,
    # a DM-delivered .env was silently REVERTED to the stale cloud copy within
    # 10 minutes - and nobody noticed until the next restart (a power outage,
    # weeks later) applied the old file. Real incident: a freshly allowed
    # transcriber user lost access after a reboot.
    cloud_note = _cloud_env_note(dest, _b2_env_object(folder_name))

    # A structurally valid file can still be the WRONG file (another project's
    # env, a truncated one). Compare against the Hub registry's required keys
    # and warn now, while /envrestore can still undo it in one command.
    missing = _missing_required_keys(folder_name, text)
    warn = ""
    if missing:
        warn = ("\n🚨 WARNING: missing required keys per projects.json: {}. "
                "If that was a mistake, send /envrestore {} to undo this "
                "delivery.".format(", ".join(missing), target))

    note = " (previous .env saved as .env.bak)" if backed_up else ""
    await msg.reply_text(
        f"Wrote {len(data)} bytes to {folder_name}/.env{note}.\n"
        f"Keys: {', '.join(key_names)}\n"
        f"Fingerprint: {fingerprint} (compare with /env_check in the Hub)\n"
        f"{cloud_note}{warn}\n"
        f"Restart that project via the Admin Hub to apply it."
    )


async def _b2_key_upload(update, context, doc):
    """Bootstrap: receive the B2 application key as a small .json DM'd by an admin
    and store it gitignored at Config.B2_KEY_PATH. Order of checks: admin (done by
    router) -> size -> valid key JSON -> persist atomically."""
    # The key json is tiny; cap at 20 KB.
    if doc.file_size and doc.file_size > 20 * 1024:
        await update.message.reply_text("Rejected: file too large to be a B2 key (>20 KB).")
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return

    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except Exception:
        await update.message.reply_text("Rejected: not valid JSON.")
        return
    if not isinstance(parsed, dict):
        await update.message.reply_text("Rejected: expected a JSON object.")
        return
    key_id = parsed.get("keyID") or parsed.get("applicationKeyId") or parsed.get("key_id")
    app_key = parsed.get("applicationKey") or parsed.get("application_key") or parsed.get("appKey")
    if not (isinstance(key_id, str) and key_id and isinstance(app_key, str) and app_key):
        await update.message.reply_text(
            'Rejected: not a B2 key (need {"keyID": "...", "applicationKey": "..."}).'
        )
        return

    try:
        _store_b2_key(key_id, app_key)
    except Exception as e:
        await update.message.reply_text(f"Failed to store key: {e}")
        return

    await update.message.reply_text("B2 application key stored - cloud transfer layer armed.")


async def cmd_armb2(update, context):
    """Arm the cloud layer from a command: /armb2 <keyID> <applicationKey>.
    The message containing the secret is deleted from the chat after storing."""
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /armb2 <keyID> <applicationKey>")
        return
    try:
        _store_b2_key(context.args[0], context.args[1])
    except Exception as e:
        await update.message.reply_text(f"Failed to store key: {e}")
        return
    try:
        await update.message.delete()  # scrub the key from the chat history
    except Exception:
        pass
    await update.effective_chat.send_message(
        "B2 application key stored - cloud transfer layer armed. "
        "(Your message was deleted to scrub the key.)"
    )


async def cmd_envrestore(update, context):
    """Undo a bad .env delivery in one command: swap <project>/.env with the
    .env.bak the delivery channel saved before overwriting. The bad file
    becomes the new .env.bak (so a restore is itself reversible), and the
    restored file is pushed to B2 so the 10-min env sync cannot re-apply the
    bad one. Takes effect at that project's next restart via the Hub.
    Usage: /envrestore <project folder or alias>"""
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /envrestore <project>  (folder name, or alias like 'transcriber'/'bus').\n"
            "Swaps that project's .env with its .env.bak - undoes the last .env delivery."
        )
        return
    target = context.args[0]
    folder_name = _resolve_env_target(target)
    if folder_name is None:
        await update.message.reply_text(f"Rejected: '{target}' is not a valid project folder name.")
        return
    target_dir = os.path.join(os.path.dirname(Config.BASE_DIR), folder_name)
    dest = os.path.join(target_dir, ".env")
    bak = dest + ".bak"
    if not os.path.isfile(bak):
        await update.message.reply_text(
            f"No {folder_name}/.env.bak found - nothing to restore.")
        return
    try:
        with open(bak, "rb") as f:
            good = f.read()
        bad = None
        if os.path.isfile(dest):
            with open(dest, "rb") as f:
                bad = f.read()
        # Restore first (atomic + fsync), only then overwrite the .bak with the
        # bad file - so a crash mid-way can never leave BOTH copies bad.
        part_path = dest + ".part"
        with open(part_path, "wb") as f:
            f.write(good)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part_path, dest)
        if bad is not None:
            with open(bak, "wb") as f:
                f.write(bad)
    except Exception as e:
        await update.message.reply_text(f"Restore failed: {e}")
        return

    fingerprint = hashlib.sha256(good).hexdigest()[:10]
    cloud_note = _cloud_env_note(dest, _b2_env_object(folder_name))
    swapped = " The replaced file is now .env.bak (run /envrestore again to swap back)." if bad is not None else ""
    await update.message.reply_text(
        f"Restored {folder_name}/.env from .env.bak ({len(good)} bytes).{swapped}\n"
        f"Fingerprint: {fingerprint} (compare with /env_check in the Hub)\n"
        f"{cloud_note}\n"
        f"Restart that project via the Admin Hub to apply it."
    )


async def cmd_pushdb(update, context):
    """On-demand DB backup: snapshot the live SQLite DB and upload it to B2."""
    if not _is_admin(update):
        return
    import cloud_sync
    if not cloud_sync.is_configured():
        await update.message.reply_text("Cloud layer not armed yet (missing B2 key).")
        return
    await update.message.reply_text("Backing up DB to cloud storage...")
    db_path = os.path.join(Config.BASE_DIR, "data", "bus_data.db")
    # Run off the event loop so a multi-MB upload doesn't freeze the poller.
    result = await asyncio.to_thread(cloud_sync.push_db_backup, db_path)
    if result:
        await update.message.reply_text(f"DB backup uploaded: {result}")
        return

    # "See server logs" was useless advice: the 10-second poll loop floods
    # monitor.log, so the failure has scrolled away before anyone can read it.
    # Report the refusal verbatim, and if the bucket is simply full, prune and
    # retry once - a full bucket is fixed by deleting, not by waiting.
    reason = cloud_sync.last_error() or "unknown error"
    await update.message.reply_text(f"DB backup failed:\n{reason}")

    if "storage cap" in reason.lower():
        await update.message.reply_text("Bucket is full - pruning old backups...")
        removed = await asyncio.to_thread(cloud_sync.prune_old_backups)
        if not removed:
            await update.message.reply_text(
                f"Prune removed nothing: {cloud_sync.last_error() or 'no eligible snapshots'}")
            return
        await update.message.reply_text(f"Pruned {removed} old backup(s). Retrying...")
        result = await asyncio.to_thread(cloud_sync.push_db_backup, db_path)
        await update.message.reply_text(
            f"DB backup uploaded: {result}" if result
            else f"Still failing:\n{cloud_sync.last_error() or 'unknown error'}")


async def cmd_b2(update, context):
    """Cloud-layer health in one message: armed? breaker? last refusal? how full?

    This exists because the server has no shell and its log tail is drowned by the
    poll loop -- without it, a silent B2 failure is undiagnosable from Telegram."""
    if not _is_admin(update):
        return
    import cloud_sync
    lines = [f"Armed: {'yes' if cloud_sync.is_configured() else 'NO (missing bucket or key)'}"]

    is_open, reason, until = cloud_sync.breaker_status()
    lines.append(f"Breaker: {'OPEN - ' + reason + ' until ' + until + ' UTC' if is_open else 'closed'}")
    lines.append(f"Last error: {cloud_sync.last_error() or 'none this run'}")

    if cloud_sync.is_configured():
        count, total, err = await asyncio.to_thread(cloud_sync.bucket_stats)
        if err:
            lines.append(f"bus-backups/: could not list - {err}")
        else:
            lines.append(f"bus-backups/: {count} object(s), {total / 1024 ** 3:.2f} GB "
                         f"of the 10 GB free tier")
    await update.message.reply_text("\n".join(lines))


async def cmd_b2prune(update, context):
    """Force the retention prune now: keep the newest N snapshots + monthly anchors.
    Usage: /b2prune [keep]  (default 20)."""
    if not _is_admin(update):
        return
    import cloud_sync
    if not cloud_sync.is_configured():
        await update.message.reply_text("Cloud layer not armed yet (missing B2 key).")
        return

    keep = 20
    if context.args:
        try:
            keep = max(1, int(context.args[0]))
        except ValueError:
            await update.message.reply_text("Usage: /b2prune [keep]  (a number, default 20)")
            return

    before = await asyncio.to_thread(cloud_sync.bucket_stats)
    await update.message.reply_text(
        f"Pruning bus-backups/ down to the newest {keep} + one anchor per month...")
    removed = await asyncio.to_thread(cloud_sync.prune_old_backups, keep)
    after = await asyncio.to_thread(cloud_sync.bucket_stats)
    if not removed:
        await update.message.reply_text(
            f"Deleted nothing: {cloud_sync.last_error() or 'nothing was eligible'}")
        return
    await update.message.reply_text(
        f"Deleted {removed} backup(s).\n"
        f"{before[0]} objects / {before[1] / 1024 ** 3:.2f} GB  ->  "
        f"{after[0]} objects / {after[1] / 1024 ** 3:.2f} GB")


async def cmd_pull(update, context):
    """On-demand model deploy: download a B2 object into src/models/ (restores the
    lost /upload_model). Usage: /pull <object_name>."""
    if not _is_admin(update):
        return
    import cloud_sync
    if not cloud_sync.is_configured():
        await update.message.reply_text("Cloud layer not armed yet (missing B2 key).")
        return
    if not context.args:
        await update.message.reply_text("Usage: /pull <object_name>  (downloads into src/models/)")
        return

    object_name = context.args[0]
    # basename() keeps the write inside src/models/ regardless of the object path.
    dest = os.path.join(Config.BASE_DIR, "src", "models", os.path.basename(object_name))
    await update.message.reply_text(f"Pulling {object_name}...")
    ok = await asyncio.to_thread(cloud_sync.pull, object_name, dest)
    if ok:
        await update.message.reply_text(f"Downloaded to src/models/{os.path.basename(object_name)}")
    else:
        await update.message.reply_text("Pull failed (see server logs).")


# Bootstrap channel: only public github.com HTTPS repos. This matches the
# "every bot is a public repo updated via the Hub" model, and a strict host +
# shape check stops the URL from being misread as a git flag or another scheme.
_GIT_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?/?$")


def _clone_repo(url, dest):
    """Blocking git clone (run via asyncio.to_thread). Shallow: we only need the
    code; the server never pushes from these working copies."""
    return subprocess.run(
        ["git", "clone", "--depth", "1", "--", url, dest],
        capture_output=True, text=True, timeout=180,
    )


async def cmd_deploy(update, context):
    """Bootstrap a NEW sibling project onto the server by cloning its PUBLIC GitHub
    repo next to the other projects. This is the one thing the Admin Hub's /update
    cannot do -- /update only fast-forwards a repo that is ALREADY on disk, it never
    clones. Ongoing updates still go through the Hub; this is first-deploy only.
    Usage: /deploy <https github url> [folder]
    """
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /deploy <https github url> [folder]\n"
            "Clones a PUBLIC GitHub repo as a new sibling project. "
            "Projects already on the server update via the Admin Hub instead."
        )
        return

    url = context.args[0]
    if not _GIT_URL_RE.match(url):
        await update.message.reply_text(
            "Rejected: only public https://github.com/<owner>/<repo> URLs are allowed."
        )
        return

    # Folder = explicit second arg, else the repo name from the URL.
    repo_name = url.rstrip("/").rsplit("/", 1)[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    folder = context.args[1] if len(context.args) > 1 else repo_name
    # Strict charset = no path traversal (no slashes, dots, drive letters).
    if not re.fullmatch(r"[A-Za-z0-9_-]+", folder):
        await update.message.reply_text(
            f"Rejected: '{folder}' is not a valid folder name (letters, digits, _ and - only)."
        )
        return

    projects_root = os.path.dirname(Config.BASE_DIR)
    dest = os.path.join(projects_root, folder)
    if os.path.exists(dest):
        await update.message.reply_text(
            f"Rejected: '{folder}' already exists. Update it via the Admin Hub, not /deploy."
        )
        return

    # The runner kills workers by matching folder names, so no sibling folder may be
    # a prefix of another (see runner_projects.json _README).
    try:
        siblings = [d for d in os.listdir(projects_root)
                    if os.path.isdir(os.path.join(projects_root, d))]
    except OSError:
        siblings = []
    clash = next((s for s in siblings
                  if s != folder and (s.startswith(folder) or folder.startswith(s))), None)
    if clash:
        await update.message.reply_text(
            f"Rejected: folder '{folder}' collides with existing '{clash}' "
            "(one name is a prefix of the other; the runner needs distinct names)."
        )
        return

    await update.message.reply_text(f"Cloning {url} -> {folder}/ ...")
    try:
        proc = await asyncio.to_thread(_clone_repo, url, dest)
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        await update.message.reply_text(f"Clone failed to start: {e}")
        return

    if proc.returncode != 0:
        # Leave no half-cloned directory behind so a retry starts clean.
        shutil.rmtree(dest, ignore_errors=True)
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        await update.message.reply_text(f"Clone failed (exit {proc.returncode}):\n{tail}")
        return

    await update.message.reply_text(
        f"Cloned into {folder}/. Next steps:\n"
        f"1) DM this project's .env with caption '{folder}'.\n"
        f"2) Register '{folder}' in the Hub's projects.json + runner_projects.json, "
        f"push, then /update the Hub.\n"
        f"3) /launch {folder} from the Hub once to build its venv; the runner then adopts it."
    )


def start_bot():
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    from telegram.error import NetworkError, TimedOut
    import logging
    import socket
    logging.basicConfig(level=logging.INFO)
    # httpx logs every Telegram API request at INFO - including the bot token
    # in the URL. Keep it out of the persistent log file.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not found in environment. Please add it to your .env file.")
        return

    # One token = one poller. A second instance would otherwise steal the token and
    # leave both deaf to a silent HTTP 409. Bind a localhost port for the process
    # lifetime; if it's already taken, another poller is live -> bail out. The OS
    # releases the port automatically when this process dies (no lock file to clean).
    global _SINGLE_INSTANCE_LOCK
    _SINGLE_INSTANCE_LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _SINGLE_INSTANCE_LOCK.bind(("127.0.0.1", 47615))
    except OSError:
        print("Error: another ETA bot instance is already running (single-instance guard). Exiting.")
        return

    print("Starting Telegram Bot...")
    # Slightly more generous network timeouts than the defaults so a brief blip on the
    # (residential) server connection doesn't abort a reply send or a getUpdates long
    # poll. get_updates_read_timeout must exceed the long-poll window (~10 s).
    app = (
        ApplicationBuilder().token(token)
        .read_timeout(30).write_timeout(30).connect_timeout(15).pool_timeout(15)
        .get_updates_read_timeout(40)
        .build()
    )
    # Admin-gated handlers MUST be registered before the catch-all: python-telegram-bot
    # runs only the first matching handler per group, so the catch-all would otherwise
    # swallow /pushdb, /pull, /armb2, and document uploads.
    app.add_handler(MessageHandler(filters.Document.ALL, document_upload_handler))
    app.add_handler(CommandHandler("pushdb", cmd_pushdb))
    app.add_handler(CommandHandler("b2", cmd_b2))
    app.add_handler(CommandHandler("b2prune", cmd_b2prune))
    app.add_handler(CommandHandler("pull", cmd_pull))
    app.add_handler(CommandHandler("armb2", cmd_armb2))
    app.add_handler(CommandHandler("deploy", cmd_deploy))
    app.add_handler(CommandHandler("envrestore", cmd_envrestore))
    app.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, bot_handler))

    # Transient network blips (httpx.ReadError / TimedOut) are routine for a long-poll
    # bot on a home connection; PTB auto-retries getUpdates, but with no error handler
    # an error raised while sending a reply dumps a full traceback to the log. Collapse
    # those to a one-line WARNING; still log genuine bugs with a full traceback.
    async def _on_error(update, context):
        if isinstance(context.error, (NetworkError, TimedOut)):
            logging.warning("Transient Telegram network blip (auto-retried): %s",
                            type(context.error).__name__)
        else:
            logging.error("Unhandled bot error", exc_info=context.error)

    app.add_error_handler(_on_error)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", action="store_true", help="Run as a Telegram bot")
    args = parser.parse_args()
    
    if args.bot:
        import log_tee
        log_tee.setup("telegram_bot")
        start_bot()
    else:
        predict_live_eta()
