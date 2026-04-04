import subprocess
import os
import time
import numpy as np
import cv2
import concurrent.futures

class ADBDevice:
    def __init__(self, device_id, adb_path="adb"):
        self.device_id = device_id
        self.adb_path = adb_path
        self.kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}

    def shell(self, command):
        cmd = [self.adb_path, "-s", self.device_id, "shell"] + command.split()
        return subprocess.run(cmd, capture_output=True, text=True, **self.kwargs)

    def tap(self, x, y):
        self.shell(f"input tap {x} {y}")

    def swipe(self, x1, y1, x2, y2, duration=1000):
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration}")

    def drag_and_drop(self, x1, y1, x2, y2, duration=3000):
        # Using swipe with long duration at start point often works as drag and drop in many Android versions
        # Or if available: input draganddrop x1 y1 x2 y2 duration
        # We'll stick to swipe with long duration as it's more compatible, 
        # but the user specifically asked for a hold.
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration}")

    def capture_screen(self):
        try:
            cmd = [self.adb_path, "-s", self.device_id, "exec-out", "screencap", "-p"]
            result = subprocess.run(cmd, capture_output=True, timeout=15, **self.kwargs)
            if result.returncode == 0 and len(result.stdout) > 100:
                img_array = np.frombuffer(result.stdout, np.uint8)
                return cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        except Exception as e:
            print(f"[{self.device_id}] Capture error: {e}")
        return None

    def find_image(self, template_path, threshold=0.8):
        screen = self.capture_screen()
        if screen is None: return None
        if not os.path.exists(template_path): return None
        
        screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        template = cv2.imread(template_path, 0)
        if template is None: return None
        
        res = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        
        if max_val >= threshold:
            h, w = template.shape
            center_x = max_loc[0] + w // 2
            center_y = max_loc[1] + h // 2
            return (center_x, center_y), max_val
        return None, max_val

    def find_image_in_region(self, template_path, region, threshold=0.8, screen=None):
        if screen is None:
            screen = self.capture_screen()
        if screen is None: return None
        if not os.path.exists(template_path): return None
        
        rx, ry, rw, rh = region
        screen_h, screen_w = screen.shape[:2]
        ryv, rxv = min(ry + rh, screen_h), min(rx + rw, screen_w)
        region_img = screen[ry:ryv, rx:rxv]
        
        region_gray = cv2.cvtColor(region_img, cv2.COLOR_BGR2GRAY)
        template = cv2.imread(template_path, 0)
        if template is None: return None
        
        res = cv2.matchTemplate(region_gray, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        
        if max_val >= threshold:
            h, w = template.shape
            center_x = rx + max_loc[0] + w // 2
            center_y = ry + max_loc[1] + h // 2
            return (center_x, center_y), max_val
        return None, max_val

def find_adb():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    locations = [
        os.path.join(script_dir, "adb", "adb.exe"),
        os.path.join(script_dir, "adb.exe"),
        "adb"
    ]
    for loc in locations:
        try:
            subprocess.run([loc, "version"], capture_output=True, timeout=2, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return loc
        except: continue
    return None

def get_devices(adb_path):
    subprocess.run([adb_path, "start-server"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    # Optional: connect to common ports if needed
    result = subprocess.run([adb_path, "devices"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    lines = result.stdout.strip().split("\n")[1:]
    devices = [line.split()[0] for line in lines if line.strip() and "device" in line]
    return devices
