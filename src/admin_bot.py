import os
import subprocess
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Dynamic Pathing: Auto-detect the project root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, '.env')
load_dotenv(ENV_PATH)

# Strict Environment Configurations
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID")
BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")

# Service name on the server
MONITOR_SERVICE_NAME = "cyprus-bus-monitor"

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
    # Note: On the server, we assume the environment is active or we use the system python if configured that way.
    # We will use 'pip' but you might need to change to '/path/to/venv/bin/pip' depending on setup.
    pip_cmd = ["pip", "install", "-r", "requirements.txt"]
    code, stdout, stderr = await run_shell(pip_cmd, cwd=BASE_DIR)
    if code != 0:
        await update.message.reply_text(f"⚠️ Warning: `pip install` had issues:\n<pre>{stderr}</pre>\nContinuing anyway...", parse_mode='HTML')
    else:
        await update.message.reply_text("✅ Dependencies up to date.")

    # 3. Native Restart
    if os.name == 'nt':
        await update.message.reply_text("🔄 Restarting Windows services via taskkill and COMPLETE_LAUNCH.bat...")
        
        # Kill all running python instances EXCEPT the admin bot itself (by name if possible, or just all python.exe)
        # Note: A simple 'taskkill /F /IM python.exe' would kill this bot mid-execution.
        # So we specifically kill the known scripts: monitor.py and predict_eta.py (or whatever your bot script is named)
        kill_monitor_cmd = ["taskkill", "/F", "/FI", "WINDOWTITLE eq Bus Monitor Orchestrator*"]
        kill_bot_cmd = ["taskkill", "/F", "/FI", "WINDOWTITLE eq Public ETA Bot*"]
        
        await run_shell(kill_monitor_cmd, cwd=BASE_DIR)
        await run_shell(kill_bot_cmd, cwd=BASE_DIR)

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

    # Nuke and Pave: Fetch all, then hard reset to origin/main
    git_commands = [
        ["git", "fetch", "origin", "main"],
        ["git", "reset", "--hard", "origin/main"]
    ]
    await _deploy_and_restart(update, "🔄 Initiating 'Nuke and Pave' update from origin/main...", git_commands)


async def rollback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rolls back the code by 1 commit."""
    if not await verify_user(update): return

    # Rollback 1 commit
    git_commands = [
        ["git", "reset", "--hard", "HEAD~1"]
    ]
    await _deploy_and_restart(update, "⏪ Initiating rollback to previous commit...", git_commands)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks the status of the services."""
    if not await verify_user(update): return
        
    if os.name == 'nt':
        status = "Local Windows Dev Mode (systemd disabled)"
    else:
        code, stdout, stderr = await run_shell(["systemctl", "is-active", MONITOR_SERVICE_NAME], cwd=BASE_DIR)
        status = stdout.strip() if code == 0 else f"Unknown ({stderr.strip()})"
    
    await update.message.reply_text(f"🤖 Admin Bot is Online.\n⚙️ Monitor Service Status: <b>{status}</b>", parse_mode='HTML')


def main():
    if not BOT_TOKEN:
        print("❌ Error: ADMIN_BOT_TOKEN not found in .env")
        return
    if not ADMIN_TELEGRAM_ID:
        print("⚠️ Warning: ADMIN_TELEGRAM_ID not set in .env. Bot will reject all commands.")

    print("🚀 Starting Admin Deployment Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("update", update_command))
    app.add_handler(CommandHandler("rollback", rollback_command))
    app.add_handler(CommandHandler("status", status_command))

    app.run_polling()

if __name__ == "__main__":
    main()