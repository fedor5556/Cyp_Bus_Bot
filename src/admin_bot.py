import os
import sys
import time
import shutil
import sqlite3
import zipfile
import subprocess
from datetime import datetime

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

# Dynamic Pathing: Auto-detect the project root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, '.env')
load_dotenv(ENV_PATH)

# Strict Environment Configurations
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID")
BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")

# Service name on the server (Linux only)
MONITOR_SERVICE_NAME = "cyprus-bus-monitor"

# Database path
DB_PATH = os.path.join(BASE_DIR, 'data', 'bus_data.db')
DB_WAL_PATH = DB_PATH + '-wal'
DB_SHM_PATH = DB_PATH + '-shm'

# Models directory
MODELS_DIR = os.path.join(BASE_DIR, 'src', 'models')

# Track bot start time for uptime calculation
BOT_START_TIME = datetime.now()


async def verify_user(update: Update) -> bool:
    """Verifies that the user sending the command is the authorized admin."""
    user_id = str(update.effective_user.id)
    allowed = str(ADMIN_TELEGRAM_ID).split(',') if ADMIN_TELEGRAM_ID else []
    if user_id not in allowed:
        await update.message.reply_text("⛔ Unauthorized. You do not have permission to run commands.")
        return False
    return True


async def run_shell(command: list, cwd: str) -> tuple[int, str, str]:
    """Helper to run shell commands safely and capture output."""
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return 1, "", str(e)


def kill_project_processes():
    """Kill ONLY Python processes belonging to THIS project directory.
    
    Strategy: The batch files activate the project's venv before running scripts,
    so the Python executable path will be something like:
      C:\\Users\\chapl\\Desktop\\...\\Bus Bot\\venv\\Scripts\\python.exe
    
    We match against the project's venv path to identify our processes.
    The admin_bot.py process is explicitly excluded so it stays alive.
    This ensures the host's own Python processes are never touched.
    """
    if os.name != 'nt':
        return 0
    
    # The venv Python executable is inside our project directory
    venv_python = os.path.join(BASE_DIR, 'venv')
    
    # Also match script names as fallback (in case system Python is used)
    script_names = ['monitor.py', 'predict_eta.py']
    
    killed = []
    try:
        # Use PowerShell to get all python.exe processes with their details
        ps_cmd = "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select-Object ProcessId, ExecutablePath, CommandLine | ConvertTo-Json"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, check=False, timeout=15
        )
        
        if result.returncode != 0 or not result.stdout.strip():
            return 1
        
        import json
        procs = json.loads(result.stdout)
        # PowerShell returns a single object (not list) when there's only 1 result
        if isinstance(procs, dict):
            procs = [procs]
        
        for proc in procs:
            pid = proc.get('ProcessId')
            exe_path = proc.get('ExecutablePath') or ''
            cmd_line = proc.get('CommandLine') or ''
            
            # Skip admin_bot (that's us!)
            if 'admin_bot' in cmd_line:
                continue
            
            # Match: python executable is inside our project's venv
            is_our_venv = venv_python.lower() in exe_path.lower()
            
            # Fallback match: command line contains one of our script names
            # This also catches zombie processes from old/moved directories
            is_our_script = any(s in cmd_line for s in script_names)
            
            if is_our_venv or is_our_script:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True, check=False, timeout=5
                    )
                    killed.append(pid)
                except Exception:
                    pass
        
        print(f"kill_project_processes: killed PIDs {killed}" if killed else "kill_project_processes: no matching processes found")
        return 0
        
    except Exception as e:
        print(f"kill_project_processes error: {e}")
        return 1


# ===================================================================
# DEPLOYMENT COMMANDS
# ===================================================================

async def _deploy_and_restart(update: Update, deploy_message: str, git_commands: list[list[str]]):
    """Core logic for applying git changes, installing deps, and restarting."""
    await update.message.reply_text(deploy_message)

    # 1. Execute Git Operations
    for cmd in git_commands:
        code, stdout, stderr = await run_shell(cmd, cwd=BASE_DIR)
        if code != 0:
            await update.message.reply_text(f"❌ Git failed on `{' '.join(cmd)}`:\n<pre>{stderr}</pre>", parse_mode='HTML')
            return False

    await update.message.reply_text("✅ Git operation successful. Installing dependencies...")

    # 2. Automated pip install
    if os.name == 'nt':
        pip_path = os.path.join(BASE_DIR, 'venv', 'Scripts', 'pip.exe')
    else:
        pip_path = os.path.join(BASE_DIR, 'venv', 'bin', 'pip')
    
    if os.path.exists(pip_path):
        pip_cmd = [pip_path, "install", "-r", "requirements.txt", "--quiet"]
    else:
        pip_cmd = ["pip", "install", "-r", "requirements.txt", "--quiet"]
    
    code, stdout, stderr = await run_shell(pip_cmd, cwd=BASE_DIR)
    if code != 0:
        await update.message.reply_text(f"⚠️ Warning: pip install had issues:\n<pre>{stderr[:500]}</pre>\nContinuing anyway...", parse_mode='HTML')
    else:
        await update.message.reply_text("✅ Dependencies up to date.")

    # 3. Native Restart
    if os.name == 'nt':
        await update.message.reply_text(f"🔄 Stopping project processes in:\n<code>{BASE_DIR}</code>", parse_mode='HTML')
        
        kill_project_processes()

        # Small delay to let processes fully terminate
        time.sleep(2)

        # Relaunch the batch files in new detached windows
        monitor_bat = os.path.join(BASE_DIR, "run_monitor.bat")
        bot_bat = os.path.join(BASE_DIR, "start_telegram_bot.bat")
        
        if os.path.exists(monitor_bat):
            subprocess.Popen(["cmd.exe", "/c", "start", "Bus Monitor Orchestrator", monitor_bat], cwd=BASE_DIR)
        if os.path.exists(bot_bat):
            subprocess.Popen(["cmd.exe", "/c", "start", "Public ETA Bot", bot_bat], cwd=BASE_DIR)
            
        await update.message.reply_text("🚀 Deployment complete! Windows processes restarted.")
        
    else:
        await update.message.reply_text("🔄 Restarting monitor service via systemd...")
        restart_cmd = ["sudo", "systemctl", "restart", MONITOR_SERVICE_NAME]
        code, stdout, stderr = await run_shell(restart_cmd, cwd=BASE_DIR)
        
        if code != 0:
            await update.message.reply_text(f"❌ Systemd restart failed:\n<pre>{stderr}</pre>\nIs the sudoers file configured correctly?", parse_mode='HTML')
            return False

        await update.message.reply_text("🚀 Deployment complete! The monitor has been restarted.")
        
    return True


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pulls the latest code from GitHub (Nuke and Pave)."""
    if not await verify_user(update): return

    git_commands = [
        ["git", "fetch", "origin", "main"],
        ["git", "reset", "--hard", "origin/main"]
    ]
    await _deploy_and_restart(update, "🔄 Initiating 'Nuke and Pave' update from origin/main...", git_commands)


async def rollback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rolls back the code by 1 commit."""
    if not await verify_user(update): return

    git_commands = [
        ["git", "reset", "--hard", "HEAD~1"]
    ]
    await _deploy_and_restart(update, "⏪ Initiating rollback to previous commit...", git_commands)


# ===================================================================
# STATUS & MONITORING COMMANDS
# ===================================================================

def _get_git_info() -> str:
    """Get the last commit hash and message."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h - %s"],
            cwd=BASE_DIR, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else "Unknown"
    except Exception:
        return "Unknown"


def _get_db_size() -> str:
    """Get the total size of all DB files."""
    total = 0
    for path in [DB_PATH, DB_WAL_PATH, DB_SHM_PATH]:
        if os.path.exists(path):
            total += os.path.getsize(path)
    if total > 1024 * 1024:
        return f"{total / (1024 * 1024):.1f} MB"
    elif total > 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total} bytes"


def _get_disk_free() -> str:
    """Get free disk space on the drive where the project lives."""
    try:
        usage = shutil.disk_usage(BASE_DIR)
        free_gb = usage.free / (1024 ** 3)
        return f"{free_gb:.1f} GB"
    except Exception:
        return "Unknown"


def _get_uptime() -> str:
    """Calculate bot uptime since start."""
    delta = datetime.now() - BOT_START_TIME
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _get_quick_row_count(table_name: str) -> int:
    """Quick row count for a single table."""
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()[0]
        conn.close()
        return count
    except Exception:
        return -1


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows comprehensive system status."""
    if not await verify_user(update): return

    commit = _get_git_info()
    db_size = _get_db_size()
    disk_free = _get_disk_free()
    uptime = _get_uptime()
    
    vp_count = _get_quick_row_count("vehicle_positions")
    se_count = _get_quick_row_count("stop_events")

    msg = (
        "🤖 <b>Admin Bot Online</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Last Commit: <code>{commit}</code>\n"
        f"⏱ Bot Uptime: {uptime}\n"
        f"💾 DB Size: {db_size}\n"
        f"📊 GPS Pings: {vp_count:,}\n"
        f"📊 Stop Events: {se_count:,}\n"
        f"💿 Disk Free: {disk_free}\n"
        f"🖥 Platform: {'Windows' if os.name == 'nt' else 'Linux'}"
    )
    await update.message.reply_text(msg, parse_mode='HTML')


async def dbstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows detailed database statistics."""
    if not await verify_user(update): return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        tables_info = []
        tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

        for (table_name,) in tables:
            count = cursor.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()[0]
            tables_info.append(f"  • <code>{table_name}</code>: <b>{count:,}</b> rows")

        # Date ranges for key tables
        ranges = []
        try:
            r = cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM vehicle_positions").fetchone()
            if r[0]:
                ranges.append(f"📍 GPS Pings: {r[0][:10]} → {r[1][:10]}")
        except Exception:
            pass

        try:
            r = cursor.execute("SELECT MIN(actual_arrival_time), MAX(actual_arrival_time) FROM stop_events").fetchone()
            if r[0]:
                ranges.append(f"🚏 Stop Events: {r[0][:10]} → {r[1][:10]}")
        except Exception:
            pass

        try:
            r = cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM weather_records").fetchone()
            if r[0]:
                ranges.append(f"🌤 Weather: {r[0][:10]} → {r[1][:10]}")
        except Exception:
            pass

        # Events with/without delay
        try:
            total = cursor.execute("SELECT COUNT(*) FROM stop_events").fetchone()[0]
            with_delay = cursor.execute("SELECT COUNT(*) FROM stop_events WHERE delay_seconds IS NOT NULL").fetchone()[0]
            ranges.append(f"📐 Delays Calculated: {with_delay}/{total}")
        except Exception:
            pass

        conn.close()

        msg = (
            "📊 <b>Database Statistics</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"💾 DB Size: {_get_db_size()}\n\n"
            "<b>Row Counts:</b>\n" +
            "\n".join(tables_info) + "\n\n"
            "<b>Date Ranges:</b>\n" +
            "\n".join(ranges)
        )
        await update.message.reply_text(msg, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"❌ Database error:\n<pre>{str(e)[:500]}</pre>", parse_mode='HTML')


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the last 30 lines of monitor output (if logging to file)."""
    if not await verify_user(update): return
    
    # Check for common log locations
    log_paths = [
        os.path.join(BASE_DIR, 'logs', 'monitor.log'),
        os.path.join(BASE_DIR, 'monitor.log'),
    ]
    
    log_file = None
    for path in log_paths:
        if os.path.exists(path):
            log_file = path
            break
    
    if not log_file:
        # No log file - try to get recent StopEvents as a proxy for "is it working?"
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute(
                "SELECT vehicle_id, trip_id, actual_arrival_time FROM stop_events "
                "ORDER BY actual_arrival_time DESC LIMIT 5"
            ).fetchall()
            conn.close()
            
            if rows:
                lines = ["📋 <b>Recent Stop Events</b> (no log file found)\n"]
                for r in rows:
                    lines.append(f"  🚌 {r[0]} | trip {r[1]} | {r[2][:19]}")
                msg = "\n".join(lines)
            else:
                msg = "📋 No log file found and no stop events recorded yet."
        except Exception as e:
            msg = f"📋 No log file found. DB query error: {str(e)[:200]}"
    else:
        try:
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            last_30 = lines[-30:] if len(lines) > 30 else lines
            content = "".join(last_30).strip()
            # Telegram message limit is 4096 chars
            if len(content) > 3800:
                content = content[-3800:]
            msg = f"📋 <b>Last {len(last_30)} log lines:</b>\n<pre>{content}</pre>"
        except Exception as e:
            msg = f"❌ Error reading log: {str(e)[:300]}"
    
    await update.message.reply_text(msg, parse_mode='HTML')


# ===================================================================
# DATA SYNC COMMANDS
# ===================================================================

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compresses and sends the database as a ZIP file via Telegram."""
    if not await verify_user(update): return

    if not os.path.exists(DB_PATH):
        await update.message.reply_text("❌ Database file not found!")
        return

    await update.message.reply_text("📦 Preparing database backup...\nThis may take a moment for large databases.")

    try:
        # Create a temp directory for the backup
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        temp_dir = os.path.join(BASE_DIR, 'data', f'_backup_temp_{timestamp}')
        os.makedirs(temp_dir, exist_ok=True)

        # Use SQLite's built-in backup to create a consistent copy
        # This is safe even while the DB is being written to
        src_conn = sqlite3.connect(DB_PATH)
        backup_db_path = os.path.join(temp_dir, 'bus_data.db')
        dst_conn = sqlite3.connect(backup_db_path)
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()

        # ZIP the backup
        zip_filename = f"bus_data_backup_{timestamp}.zip"
        zip_path = os.path.join(temp_dir, zip_filename)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(backup_db_path, 'bus_data.db')
        
        # Get sizes for the status message
        db_size_mb = os.path.getsize(backup_db_path) / (1024 * 1024)
        zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)

        # Send via Telegram (supports up to 2 GB)
        await update.message.reply_text(f"📤 Uploading backup ({zip_size_mb:.1f} MB compressed from {db_size_mb:.1f} MB)...")
        
        with open(zip_path, 'rb') as f:
            await update.message.reply_document(
                document=f,
                filename=zip_filename,
                caption=f"🗄 Database Backup\n📅 {timestamp}\n💾 {db_size_mb:.1f} MB → {zip_size_mb:.1f} MB compressed"
            )

        # Cleanup temp files
        shutil.rmtree(temp_dir, ignore_errors=True)

        await update.message.reply_text("✅ Backup complete!")

    except Exception as e:
        # Cleanup on error
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir, ignore_errors=True)
        await update.message.reply_text(f"❌ Backup failed:\n<pre>{str(e)[:500]}</pre>", parse_mode='HTML')


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accepts an uploaded database file and replaces the current one."""
    if not await verify_user(update): return

    await update.message.reply_text(
        "📥 <b>Database Restore</b>\n\n"
        "Send me a <code>.db</code> or <code>.zip</code> file containing the database.\n"
        "I will stop the monitor, replace the database, and restart everything.\n\n"
        "⚠️ <b>This will overwrite the current database!</b>",
        parse_mode='HTML'
    )


async def upload_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accepts an uploaded ML model file."""
    if not await verify_user(update): return

    await update.message.reply_text(
        "🧠 <b>Upload ML Model</b>\n\n"
        "Send me a model file (<code>.pkl</code>, <code>.joblib</code>, or <code>.json</code>).\n"
        "It will be saved to <code>src/models/</code> and services will be restarted.",
        parse_mode='HTML'
    )


async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming file uploads for /restore and /upload_model."""
    if not await verify_user(update): return

    document = update.message.document
    if not document:
        return

    filename = document.file_name or "unknown"
    file_ext = os.path.splitext(filename)[1].lower()

    # Determine if this is a DB restore or model upload
    if file_ext in ('.db', '.zip') or 'bus_data' in filename.lower() or 'backup' in filename.lower():
        await _handle_db_restore(update, context, document, filename, file_ext)
    elif file_ext in ('.pkl', '.joblib', '.json', '.onnx'):
        await _handle_model_upload(update, context, document, filename)
    else:
        await update.message.reply_text(
            f"❓ Unknown file type: <code>{file_ext}</code>\n\n"
            "Supported:\n"
            "• <code>.db</code> / <code>.zip</code> → Database restore\n"
            "• <code>.pkl</code> / <code>.joblib</code> / <code>.json</code> → ML model upload",
            parse_mode='HTML'
        )


async def _handle_db_restore(update, context, document, filename, file_ext):
    """Process a database restore from an uploaded file."""
    await update.message.reply_text(f"📥 Downloading <code>{filename}</code>...", parse_mode='HTML')

    try:
        # Download the file
        file = await document.get_file()
        temp_dir = os.path.join(BASE_DIR, 'data', '_restore_temp')
        os.makedirs(temp_dir, exist_ok=True)
        download_path = os.path.join(temp_dir, filename)
        await file.download_to_drive(download_path)

        # If ZIP, extract it
        db_source_path = download_path
        if file_ext == '.zip':
            await update.message.reply_text("📦 Extracting ZIP...")
            with zipfile.ZipFile(download_path, 'r') as zf:
                # Find the .db file inside
                db_files = [f for f in zf.namelist() if f.endswith('.db')]
                if not db_files:
                    await update.message.reply_text("❌ No .db file found inside the ZIP!")
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return
                zf.extract(db_files[0], temp_dir)
                db_source_path = os.path.join(temp_dir, db_files[0])

        # Stop services
        await update.message.reply_text("🛑 Stopping monitor and ETA bot...")
        if os.name == 'nt':
            kill_project_processes()
        time.sleep(2)

        # Backup current DB
        if os.path.exists(DB_PATH):
            backup_name = DB_PATH + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(DB_PATH, backup_name)
            await update.message.reply_text(f"💾 Current DB backed up to <code>{os.path.basename(backup_name)}</code>", parse_mode='HTML')

        # Remove old WAL/SHM files
        for f in [DB_WAL_PATH, DB_SHM_PATH]:
            if os.path.exists(f):
                os.remove(f)

        # Replace DB
        shutil.copy2(db_source_path, DB_PATH)
        
        # Cleanup temp
        shutil.rmtree(temp_dir, ignore_errors=True)

        # Restart services
        await update.message.reply_text("🔄 Restarting services...")
        if os.name == 'nt':
            monitor_bat = os.path.join(BASE_DIR, "run_monitor.bat")
            bot_bat = os.path.join(BASE_DIR, "start_telegram_bot.bat")
            if os.path.exists(monitor_bat):
                subprocess.Popen(["cmd.exe", "/c", "start", "Bus Monitor Orchestrator", monitor_bat], cwd=BASE_DIR)
            if os.path.exists(bot_bat):
                subprocess.Popen(["cmd.exe", "/c", "start", "Public ETA Bot", bot_bat], cwd=BASE_DIR)

        new_size = os.path.getsize(DB_PATH) / (1024 * 1024)
        await update.message.reply_text(
            f"✅ Database restored successfully!\n"
            f"💾 New DB size: {new_size:.1f} MB\n"
            f"🚀 Services restarted."
        )

    except Exception as e:
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir, ignore_errors=True)
        await update.message.reply_text(f"❌ Restore failed:\n<pre>{str(e)[:500]}</pre>", parse_mode='HTML')


async def _handle_model_upload(update, context, document, filename):
    """Process an ML model file upload."""
    await update.message.reply_text(f"📥 Downloading model <code>{filename}</code>...", parse_mode='HTML')

    try:
        # Ensure models directory exists
        os.makedirs(MODELS_DIR, exist_ok=True)

        # Download file
        file = await document.get_file()
        save_path = os.path.join(MODELS_DIR, filename)
        await file.download_to_drive(save_path)

        file_size = os.path.getsize(save_path) / (1024 * 1024)
        
        await update.message.reply_text(
            f"✅ Model saved!\n"
            f"📁 Location: <code>src/models/{filename}</code>\n"
            f"💾 Size: {file_size:.1f} MB\n\n"
            f"Send /update to restart services and load the new model.",
            parse_mode='HTML'
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Upload failed:\n<pre>{str(e)[:500]}</pre>", parse_mode='HTML')


# ===================================================================
# HELP COMMAND
# ===================================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows all available commands."""
    if not await verify_user(update): return

    msg = (
        "🤖 <b>Admin Bot Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Deployment:</b>\n"
        "  /update or /deploy — Pull latest code, restart\n"
        "  /rollback — Revert to previous commit\n\n"
        "<b>Monitoring:</b>\n"
        "  /status — System overview\n"
        "  /dbstats — Detailed database statistics\n"
        "  /logs — Recent activity\n\n"
        "<b>Data Sync:</b>\n"
        "  /backup — Download database as ZIP\n"
        "  /restore — Upload a database file\n"
        "  /upload_model — Upload an ML model file\n"
        "  <i>(or just send any .db/.zip/.pkl file)</i>\n\n"
        "<b>Other:</b>\n"
        "  /help — Show this message"
    )
    await update.message.reply_text(msg, parse_mode='HTML')


# ===================================================================
# MAIN
# ===================================================================

def main():
    if not BOT_TOKEN:
        print("❌ Error: ADMIN_BOT_TOKEN not found in .env")
        return
    if not ADMIN_TELEGRAM_ID:
        print("⚠️ Warning: ADMIN_TELEGRAM_ID not set in .env. Bot will reject all commands.")

    print("🚀 Starting Admin Deployment Bot...")
    print(f"   Project root: {BASE_DIR}")
    print(f"   Database: {DB_PATH}")
    print(f"   Authorized admins: {ADMIN_TELEGRAM_ID}")
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Deployment commands
    app.add_handler(CommandHandler("update", update_command))
    app.add_handler(CommandHandler("deploy", update_command))  # Alias
    app.add_handler(CommandHandler("rollback", rollback_command))

    # Monitoring commands
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("dbstats", dbstats_command))
    app.add_handler(CommandHandler("logs", logs_command))

    # Data sync commands
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(CommandHandler("upload_model", upload_model_command))

    # Help
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", help_command))

    # File upload handler (auto-detects DB vs model files)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file_upload))

    print("✅ Bot is running. Waiting for commands...")
    app.run_polling()


if __name__ == "__main__":
    main()