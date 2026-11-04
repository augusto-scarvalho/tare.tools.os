import os
import subprocess
import sys
import time

out = subprocess.check_output('wmic process get processid,commandline', shell=True, text=True, errors='replace')
my_pid = os.getpid()

for line in out.splitlines():
    if 'cluster_coordinator.py' in line and str(my_pid) not in line:
        parts = line.strip().split()
        if parts:
            pid = parts[-1]
            if pid.isdigit() and int(pid) != my_pid:
                print(f"Killing old coordinator PID {pid}")
                subprocess.run(f'taskkill /F /PID {pid}', shell=True)

time.sleep(1)
print("Spawning fresh cluster_coordinator.py...")
proc = subprocess.Popen([sys.executable, "relay/cluster_coordinator.py"], cwd=os.getcwd())
print(f"Spawned with PID {proc.pid}!")
