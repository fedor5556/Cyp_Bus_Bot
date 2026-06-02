============================================================
  Cyprus Bus Analysis Server - Setup Instructions
============================================================

QUICK START:
  1. Make sure Python 3.10+ and Git are installed
  2. Double-click SETUP.bat
  3. Press Enter to accept the default install location
  4. Wait ~2 minutes for packages to install
  5. Three terminal windows will open - the server is running!

IMPORTANT:
  - Do NOT close the "Admin Deployment Bot" window
    It handles remote code updates via Telegram.
  - The "Bus Monitor Orchestrator" window collects live bus data.
  - The "Public ETA Bot" window runs the Telegram ETA bot.

IF SETUP.BAT FAILS:
  1. Open Command Prompt
  2. Run: git clone https://github.com/fedor5556/Cyp_Bus_Bot.git C:\CypBusBot
  3. Copy .env from server_data\ into C:\CypBusBot\
  4. Copy bus_data.db files from server_data\ into C:\CypBusBot\data\
  5. Run: cd C:\CypBusBot && python -m venv venv
  6. Run: venv\Scripts\pip install -r requirements.txt
  7. Double-click COMPLETE_LAUNCH.bat inside C:\CypBusBot\

NEED HELP? Message @fedor5556 on Telegram.
