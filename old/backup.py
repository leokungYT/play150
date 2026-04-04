import cv2
import numpy as np
import subprocess
import os
from time import sleep
import sys

# To avoid circular import issues if textocr imports ranger
# We define globals first
device_id = "emulator-5556"
filename = "screen.png"
adb_path = "adb" # Default, will be updated dynamically

try:
    import pytesseract
    from textocr import * 
except ImportError:
    print("Warning: textocr or pytesseract not found")

def find_adb_executable():
    global adb_path
    
    # 1. Check local adb folder
    if os.path.exists(r"adb\adb.exe"):
        adb_path = r"adb\adb.exe"
        print(f"✅ Found local ADB: {adb_path}")
        return True

    # 2. Check system PATH
    try:
        subprocess.run(["adb", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        adb_path = "adb"
        print("✅ Found ADB in system PATH")
        return True
    except FileNotFoundError:
        pass

    # 3. Check MuMu specific paths (from main.py)
    mumu_adb_paths = [
        "F:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe",
        "C:\\Program Files\\Netease\\MuMuPlayerGlobal-12.0\\shell\\adb.exe",
        "C:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe",
        "F:\\MuMuPlayerGlobal-12.0\\shell\\adb.exe",
        "D:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe",
        "E:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe"
    ]
    
    for path in mumu_adb_paths:
        if os.path.exists(path):
            adb_path = path
            print(f"✅ Found MuMu ADB: {path}")
            return True
            
    print("❌ ADB executable not found!")
    return False

def connect_known_ports():
    """Auto-scan and connect to common emulator ports"""
    print("🔄 Auto-connecting to common emulator ports...")
    
    # Common ports for MuMu, Nox, LDPlayer, BlueStacks
    target_ports = [7555, 62001] 
    
    # Scan ports 5555, 5557, 5559, ... up to 5615 (30 devices) for LDPlayer/BlueStacks
    start_port = 5555
    max_devices = 20 # Reduced from 30 to save time
    
    for i in range(max_devices): 
        target_ports.append(start_port + (i * 2))

    for port in target_ports:
        print(f"\r⏳ Connecting to 127.0.0.1:{port}...", end="", flush=True)
        cmd = [adb_path, "connect", f"127.0.0.1:{port}"]
        try:
            # Run fast with short timeout (0.5s is enough for localhost)
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=0.5)
        except:
            pass
    print("\n✅ Finished checking ports.")

def get_connected_devices():
    """Parse 'adb devices' output to get list of serials"""
    try:
        if " " in adb_path:
            # Handle path with spaces
            cmd = f'"{adb_path}" devices'
            result = subprocess.check_output(cmd, shell=True, text=True)
        else:
            result = subprocess.check_output([adb_path, "devices"], text=True)
            
        lines = result.strip().split("\n")[1:]
        devices = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except Exception as e:
        print(f"❌ Error getting devices: {e}")
        return []

def capture_screen():
    # Use proper quoting for paths with spaces
    adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
    
    # Try exec-out first (faster)
    cmd = f'{adb_cmd} -s {device_id} exec-out screencap -p > {filename}'
    os.system(cmd)
    
    if not os.path.exists(filename) or os.path.getsize(filename) == 0:
        # Fallback to shell screencap + pull
        os.system(f'{adb_cmd} -s {device_id} shell screencap -p /sdcard/screen.png')
        os.system(f'{adb_cmd} -s {device_id} pull /sdcard/screen.png {filename}')
        
    if not os.path.exists(filename):
         # raise Exception(f"❌ Capture screen failed for {device_id}")
         print(f"❌ Capture screen failed for {device_id}")

def find(template_path, similarity=0.8):
    capture_screen()
    if not os.path.exists(filename): return None
    
    img = cv2.imread(filename, 0)
    template = cv2.imread(template_path, 0)

    if img is None or template is None:
        # print(f"❌ ไม่พบภาพหรือเทมเพลต: {template_path}")
        return None

    result = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)
    loc = np.where(result >= similarity)
    if len(loc[0]) > 0:
        y, x = loc[0][0], loc[1][0]
        h, w = template.shape
        return x+w//2, y+h//2
    return None

def exists(template_path, similarity=0.8):
    return find(template_path, similarity) is not None

def click(PSMRL, similarity=0.8):
    target = None
    
    if isinstance(PSMRL, str): 
        if os.path.exists(PSMRL):
            # pattern image path
            target = find(PSMRL, similarity)
        else:
            # print(f"Image not found: {PSMRL}")
            pass
    elif isinstance(PSMRL, tuple) and len(PSMRL)==2:
        # (x,y)
        target = PSMRL

    if target:
        x, y = target
        adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
        subprocess.run([
            adb_cmd, "-s", device_id, "shell",
            "input", "tap", str(x), str(y)
        ])
        return 1
    else:
        return 0

def swipe(start_x, start_y, end_x, end_y, duration_ms=300):
    adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
    os.system(f'{adb_cmd} -s {device_id} shell input swipe {start_x} {start_y} {end_x} {end_y} {duration_ms}')

def main_login():
    print(f"[{device_id}] Starting main_login...")
    
    # 1. Click Icon (Optional, if we are at home screen)
    if exists(r"img\icon.png"):
        print(f"[{device_id}] Found icon, entering game...")
        click(r"img\icon.png")
        sleep(5)

    # 2. Main Loop
    loop_count = 0
    # Loop until stoplogin found
    while not exists(r"img\stoplogin.png"):
        loop_count += 1
        if loop_count % 5 == 0:
            print(f"[{device_id}] Main Loop #{loop_count} running...")
            
        # Check for event
        if exists(r"img\event.png"):
            print(f"[{device_id}] Found event.png, handling event...")
            click(r"img\event.png")
            sleep(1)
            
            # Press back repeatedly until cancel.png found
            back_attempts = 0
            adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
            
            while not exists(r"img\cancel.png"):
                # print(f"[{device_id}] Pressing BACK...")
                subprocess.run([adb_cmd, "-s", device_id, "shell", "input", "keyevent", "4"])
                sleep(0.8)
                back_attempts += 1
                
                # Safety checks
                if exists(r"img\stoplogin.png"):
                    print(f"[{device_id}] Found stoplogin.png inside cancel loop!")
                    return # Exit function directly logic
                
                if back_attempts > 20: 
                    print(f"[{device_id}] Too many back attempts, breaking cancel loop...")
                    break
        
        sleep(1)
        
        # Safety break to avoid infinite loop if nothing happens for too long
        if loop_count > 500:
             print(f"[{device_id}] ⚠️ Max loops reached.")
             break

    print(f"[{device_id}] Found stoplogin.png! Clearing app...")
    adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
    subprocess.run([adb_cmd, "-s", device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
    print(f"[{device_id}] Login sequence finished!")

if __name__ == "__main__":
    print("=== Auto ADB Ranger Script ===")
    
    # 1. auto find adb
    if not find_adb_executable():
        print("Cannot continue without ADB.")
        sys.exit(1)

    # 2. auto connect ports
    connect_known_ports()
    
    # 3. get devices
    devices = get_connected_devices()
    print(f"📱 Connected devices: {devices}")
    
    if not devices:
        print("❌ No devices found.")
        sys.exit(0)

    # 4. run main_login for each device
    for dev in devices:
        print(f"\n========================================")
        print(f"▶ Processing Device: {dev}")
        print(f"========================================")
        
        # Update globals for this device
        device_id = dev
        filename = f"screen-{dev.replace(':', '_')}.png" # Unique filename per device
        
        try:
            main_login()
        except Exception as e:
            print(f"❌ Error processing {dev}: {e}")
            
    print("\n✅ All devices processed.")
