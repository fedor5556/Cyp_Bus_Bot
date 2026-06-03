"""Kills Python processes belonging to this project.

Two modes:
1. Kills any python.exe running from this project's venv (safe, path-matched)
2. Kills any python.exe running our specific scripts (monitor.py, predict_eta.py)
   from ANY directory — this catches zombie processes from old/moved folders
"""
import subprocess
import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(PROJECT_DIR, 'venv')

# Script names unique to this project — safe to kill from any directory
OUR_SCRIPTS = ['monitor.py', 'predict_eta.py', 'fetch_rt.py', 'fetch_static.py']

def main():
    print(f"Project: {PROJECT_DIR}\n")
    
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
             "| Select-Object ProcessId, ExecutablePath, CommandLine "
             "| ConvertTo-Json"],
            capture_output=True, text=True, check=False, timeout=15
        )
    except Exception as e:
        print(f"Error querying processes: {e}")
        return
    
    if not result.stdout.strip():
        print("No python.exe processes running.")
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
        
        if pid == my_pid:
            continue
            
        # Skip admin_bot — we need it alive
        if 'admin_bot' in cmd:
            continue
        
        # Match 1: Python executable is inside our project's venv
        is_our_venv = VENV_DIR.lower() in exe.lower()
        
        # Match 2: Command line contains one of our unique script names
        #          (catches zombies from old/moved directories)
        is_our_script = any(script in cmd for script in OUR_SCRIPTS)
        
        if is_our_venv or is_our_script:
            tag = "VENV" if is_our_venv else "ZOMBIE"
            print(f"  [{tag}] PID {pid}: {cmd.strip()}")
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, check=False)
            killed += 1
    
    if killed == 0:
        print("No Bus Bot processes found.")
    else:
        print(f"\nStopped {killed} process(es). Your other programs are untouched.")

if __name__ == "__main__":
    main()
