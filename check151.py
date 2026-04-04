import cv2
import numpy as np
import subprocess
import time
import os

# ADB Configuration
adb_path = "adb"
device_id = "127.0.0.1:5557" # Default port, can be changed
backup_id_dir = "backup-id"

if not os.path.exists(backup_id_dir):
    os.makedirs(backup_id_dir)

def capture_screen():
    try:
        result = subprocess.run(
            [adb_path, "-s", device_id, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=15
        )
        if result.returncode == 0 and len(result.stdout) > 100:
            img_array = np.frombuffer(result.stdout, np.uint8)
            img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            return img
    except Exception as e:
        print(f"Error capturing screen: {e}")
    return None

def find_image(screen, template_path, threshold=0.9):
    if screen is None or not os.path.exists(template_path):
        return None
    template = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if template is None: return None
    res = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    loc = np.where(res >= threshold)
    if len(loc[0]) > 0:
        return True
    return False

def pull_account():
    remote_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
    timestamp = int(time.time())
    dest_name = f"stage151_test_{timestamp}.xml"
    dest_path = os.path.join(backup_id_dir, dest_name)
    
    print(f"[{device_id}] Stage 151 detected! Pulling account to {dest_path}...")
    # Fix permissions before pulling
    subprocess.run([adb_path, "-s", device_id, "shell", "su", "-c", f"chmod 666 {remote_path}"])
    subprocess.run([adb_path, "-s", device_id, "pull", remote_path, dest_path])
    print(f"[{device_id}] Backup COMPLETED.")

def main():
    print(f"Monitoring {device_id} for Stage 151 (Only Check and Pull Mode)...")
    while True:
        screen = capture_screen()
        if screen is not None:
            # Check for stage151.png or quest-stage1.png
            if find_image(screen, "img/stage151.png", threshold=0.95) or \
               find_image(screen, "img/quest-stage1.png", threshold=0.9):
                pull_account()
                print("Detection successful! Stopping script as requested (one-shot mode).")
                break # Exit after backup
        
        time.sleep(2)

if __name__ == "__main__":
    main()
