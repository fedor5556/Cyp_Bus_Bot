"""Kills ONLY Python processes belonging to this project's venv.
Does NOT touch any other Python processes on the system."""
import subprocess
import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(PROJECT_DIR, 'venv')

def main():
    print(f"Project: {PROJECT_DIR}")
    print(f"Scanning for python.exe processes in this project's venv...\n")
    
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select-Object ProcessId, ExecutablePath, CommandLine | ConvertTo-Json"],
            capture_output=True, text=True, check=False, timeout=15
        )
    except Exception as e:
        print(f"Error querying processes: {e}")
        return
    
    if not result.stdout.strip():
        print("No python.exe processes found at all.")
        return
    
    procs = json.loads(result.stdout)
    if isinstance(procs, dict):
        procs = [procs]
    
    killed = 0
    my_pid = os.getpid()
    
    for proc in procs:
        pid = proc.get('ProcessId')
        exe = proc.get('ExecutablePath') or ''
        cmd = proc.get('CommandLine') or ''
        
        # Don't kill ourselves
        if pid == my_pid:
            continue
        
        # Only kill if the executable is inside our project's venv
        if VENV_DIR.lower() in exe.lower():
            print(f"  [KILL] PID {pid}: {cmd.strip()}")
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, check=False)
            killed += 1
        else:
            # Skip silently — not our process
            pass
    
    if killed == 0:
        print("No Bus Bot processes found running.")
    else:
        print(f"\nStopped {killed} process(es). Your other programs are untouched.")

if __name__ == "__main__":
    main()
