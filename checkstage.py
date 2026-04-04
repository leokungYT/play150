import cv2
import numpy as np
import time
import re
import os
import subprocess
import shutil
import concurrent.futures
from threading import Thread
import gc
import getpass
import random
import json
import queue
import tempfile
import glob
from queue import Queue
import math
from scanner import HeroScanner

class AccountFinished(Exception):
    """Raised when an account reached Stage 151 and needs backup/switching."""
    pass

class GameCrashed(Exception):
    """Raised when the game process is no longer detected."""
    pass

account_queue = Queue()


# ADB Configuration
adb_path = "adb"

def find_adb_executable():
    global adb_path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    adb_locations = [
        os.path.join(script_dir, "adb", "adb.exe"),
        os.path.join(script_dir, "adb", "adb"),
        os.path.join(os.getcwd(), "adb", "adb.exe"),
    ]
    for loc in adb_locations:
        if os.path.exists(loc):
            try:
                kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
                result = subprocess.run([loc, "version"], capture_output=True, text=True, timeout=5, shell=(os.name == 'nt'), **kwargs)
                if result.returncode == 0:
                    adb_path = loc
                    return True
            except: pass
    adb_in_path = shutil.which("adb")
    if adb_in_path:
        adb_path = os.path.abspath(adb_in_path)
        return True
    return False

def connect_known_ports():
    try:
        kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        subprocess.run([adb_path, "kill-server"], capture_output=True, timeout=3, **kwargs)
        time.sleep(0.1)
        subprocess.run([adb_path, "start-server"], capture_output=True, timeout=3, **kwargs)
        time.sleep(0.5)
        ports = list(range(5555, 5756, 2))
        def try_connect_port(port):
            try:
                addr = f"127.0.0.1:{port}"
                subprocess.run([adb_path, "connect", addr], capture_output=True, timeout=1, text=True, **kwargs)
            except: pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            executor.map(try_connect_port, ports)
    except: pass

def get_connected_devices():
    try:
        kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=10, **kwargs)
        lines = result.stdout.strip().split("\n")[1:]
        raw_list = []
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                raw_list.append(parts[0])

        # Deduplicate: prefer 127.0.0.1:port over emulator-port
        port_map = {}
        for serial in raw_list:
            port = None
            if "127.0.0.1:" in serial:
                try: port = int(serial.split(":")[1])
                except: pass
            elif "emulator-" in serial:
                try: port = int(serial.split("-")[1]) + 1
                except: pass

            if port:
                if port not in port_map or "127.0.0.1" in serial:
                    port_map[port] = serial
            else:
                port_map[serial] = serial

        return list(port_map.values())
    except: return []

class BotInstance:
    def __init__(self, device_id):
        self.device_id = device_id
        self.adb_cmd = adb_path
        self.kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        self.screen_bgr = None
        self.screen_gray = None
        self._ocr_reader = None
        self.in_quest_routine = False
        self.config = {}
        self.current_account = None
        self.last_activity_time = time.time()
        self.is_first_31 = True
        self.check_pid_timer = 0
        self._fixnetv3_count = 0
        self._need_restart = False
        self._in_popup_check = False
        self._checklv_done = False
        self._login_fixid_count = 0

    def load_config(self):
        try:
            with open("configmain.json", "r") as f:
                self.config = json.load(f)
        except:
            self.config = {"getclearquest": 0}

    def push_file(self, local_path, remote_path="/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"):
        print(f"[{self.device_id}] Pushing {local_path} to {remote_path} (Robust Mode)...")
        
        # Ensure directories exist
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", "mkdir -p /data/data/com.linecorp.LGRGS/shared_prefs"], **self.kwargs)
        
        # Stop app first to ensure no old data is held in memory
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
        time.sleep(1)
        
        # Delete existing file first as per user request
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", f"rm -f {remote_path} && sync"], **self.kwargs)
        
        # Robust injection: push to temp and move with su
        tmp_remote = f"/data/local/tmp/temp_pref_{self.device_id.replace(':','_')}.xml"
        
        # 1. Push to temp location
        res_push = subprocess.run([self.adb_cmd, "-s", self.device_id, "push", local_path, tmp_remote], **self.kwargs)
        
        # 2. Move to final location and set permissions
        move_cmd = (
            f"su -c '"
            f"rm -f {remote_path}; "
            f"cp {tmp_remote} {remote_path} && "
            f"chmod 666 {remote_path} && "
            f"chown $(stat -c %u:%g /data/data/com.linecorp.LGRGS/shared_prefs 2>/dev/null || echo 1000:1000) {remote_path} && "
            f"rm -f {tmp_remote} && "
            f"sync"
            f"'"
        )
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", move_cmd], **self.kwargs)
        
        # Final Verification
        time.sleep(1)
        check = subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", f"ls {remote_path}"], capture_output=True, text=True, **self.kwargs)
        if "_LINE_COCOS_PREF_KEY.xml" in check.stdout:
            print(f"[{self.device_id}] ✓ Injection confirmed.")
        else:
            print(f"[{self.device_id}] ✗ Injection FAILED! File not found in target directory.")

    def pull_file(self, local_path):
        """Pull file from device using login.py's robust method:
        Copy to /data/local/tmp/ first, then pull from there to avoid permission issues."""
        src_remote = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        temp_remote = f"/data/local/tmp/pull_pref_{self.device_id.replace(':','_')}.xml"
        
        try:
            # 1. Copy to temp with su (bypass shared_prefs permission)
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", 
                           f"su -c 'cp {src_remote} {temp_remote}'"], **self.kwargs)
            
            # 2. Set permissions so adb pull can read it
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", 
                           f"su -c 'chmod 666 {temp_remote}'"], **self.kwargs)
            
            # 3. Pull from temp location
            print(f"[{self.device_id}] Pulling {src_remote} -> {local_path} (via {temp_remote})...")
            result = subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", temp_remote, local_path], 
                                   capture_output=True, text=True, **self.kwargs)
            
            if result.returncode == 0:
                print(f"[{self.device_id}] ✓ File pulled successfully to {local_path}")
            else:
                print(f"[{self.device_id}] ✗ Pull failed: {result.stderr}")
            
            # 4. Clean up temp file
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", 
                           f"su -c 'rm -f {temp_remote}'"], **self.kwargs)
                           
        except Exception as e:
            print(f"[{self.device_id}] Pull file error: {e}")
            # Fallback: try direct pull with chmod
            try:
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", 
                               "su", "-c", f"chmod 666 {src_remote}"], **self.kwargs)
                subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", src_remote, local_path], **self.kwargs)
                print(f"[{self.device_id}] Fallback pull completed.")
            except Exception as e2:
                print(f"[{self.device_id}] Fallback pull also failed: {e2}")

    def adb_run(self, cmd_list, timeout=15):
        """Helper to run adb commands with uniform settings"""
        return subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout, **self.kwargs)

    def open_app(self):
        self.last_activity_time = time.time()
        """เปิดแอป LINE Rangers ด้วยคำสั่ง am start / monkey (เร็กว่าคลิก icon.png)"""
        attempt = 0
        while attempt < 5:
            attempt += 1
            try:
                # สลับวิธีเปิด: am start กับ monkey
                if attempt % 2 == 1:
                    print(f"[{self.device_id}] Opening app via am start (attempt {attempt})...")
                    self.adb_run([
                        self.adb_cmd, "-s", self.device_id, "shell",
                        "am", "start", "-S", "-n",
                        "com.linecorp.LGRGS/com.linecorp.common.activity.LineActivity"
                    ], timeout=10)
                else:
                    print(f"[{self.device_id}] Opening app via monkey (attempt {attempt})...")
                    self.adb_run([
                        self.adb_cmd, "-s", self.device_id, "shell",
                        "monkey", "-p", "com.linecorp.LGRGS",
                        "-c", "android.intent.category.LAUNCHER", "1"
                    ], timeout=10)
                
                time.sleep(10)
                
                # ตรวจว่าแอปยังรันอยู่ด้วย pidof
                try:
                    pid_result = self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "pidof", "com.linecorp.LGRGS"], timeout=5)
                    pid = pid_result.stdout.strip()
                except Exception:
                    pid = ""
                
                if pid:
                    print(f"[{self.device_id}] ✓ App running (PID: {pid}) - attempt {attempt}")
                    return True
                else:
                    print(f"[{self.device_id}] ✗ App crashed/bounced! (attempt {attempt}) Retrying...")
                    time.sleep(2)
                    
            except Exception as e:
                print(f"[{self.device_id}] Error opening app (attempt {attempt}): {e}")
                time.sleep(2)
        
        print(f"[{self.device_id}] Failed to open app after 5 attempts!")
        return False

    def main_login(self, current_filename=None):
        print(f"[{self.device_id}] Starting Main Login...")
        self._login_fixid_count = 0
        
        # Clear app
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
        time.sleep(2)
        
        if not self.open_app():
            print(f"[{self.device_id}] Login ABORTED: Failed to open app.")
            return False

        start_time = time.time()
        while True:
            self.capture_screen()
            
            # Check for stoplogin - indicate login finished
            if self.find_image("img/stoplogin.png", threshold=0.8):
                print(f"[{self.device_id}] Found stoplogin.png! Login complete.")
                break
                
            # --- Persistence Checks for alert2 and fixokk ---
            if self.exists_in_cache("img/alert2.png", threshold=0.8):
                if not hasattr(self, '_alert2_start_time') or self._alert2_start_time is None:
                    self._alert2_start_time = time.time()
                    print(f"[{self.device_id}] Detected alert2.png (8s wait to restart)...")
                elif time.time() - self._alert2_start_time >= 8:
                    print(f"[{self.device_id}] alert2.png persisted 8s! Restarting app/account...")
                    self._alert2_start_time = None
                    return "restart"
            else:
                self._alert2_start_time = None

            if self.exists_in_cache("img/fixokk.png", threshold=0.8):
                if not hasattr(self, '_fixokk_start_time') or self._fixokk_start_time is None:
                    self._fixokk_start_time = time.time()
                    print(f"[{self.device_id}] Detected fixokk.png (5s wait to click)...")
                elif time.time() - self._fixokk_start_time >= 5:
                    print(f"[{self.device_id}] fixokk.png persisted 5s! Clicking...")
                    self.click("img/fixokk.png")
                    self._fixokk_start_time = None
            else:
                self._fixokk_start_time = None

            # === fixid1.png → failed ทันที ===
            if self.exists_in_cache("img/fixid1.png", threshold=0.95):
                print(f"[{self.device_id}] Found fixid1.png! -> login-failed immediately")
                self._login_fixid_count = 0
                self.handle_login_failure()
                return "failed"

            # === fixid.png Check (เช็คทุกรอบ) -> fikcheck -> refresh -> check ===
            if self.exists_in_cache("img/fixid.png", threshold=0.95):
                self._login_fixid_count += 1
                print(f"[{self.device_id}] Found fixid.png ({self._login_fixid_count}/8), fikcheck -> refresh -> check...")
                
                if self._login_fixid_count >= 8:
                    print(f"[{self.device_id}] fixid limit reached (8 times)! Failing...")
                    self._login_fixid_count = 0
                    self.handle_login_failure()
                    return "failed"
                
                # 1) กด fikcheck
                print(f"[{self.device_id}] Step 1: waiting for fikcheck.png (10s timeout)...")
                time.sleep(1.5) # ให้หน้าจอเสถียรหลัง re-route
                for _ in range(10): # Timeout 10s
                    self.capture_screen()
                    if self.exists_in_cache("img/fikcheck.png", threshold=0.8):
                        self.click("img/fikcheck.png", threshold=0.8)
                        print(f"[{self.device_id}] Clicked fikcheck.png")
                        time.sleep(2)
                        break
                    time.sleep(1)
                
                # 2) กด refresh
                print(f"[{self.device_id}] Step 2: clicking refresh.png (10s timeout)...")
                for _ in range(10): # Timeout 10s
                    self.capture_screen()
                    if self.exists_in_cache("img/refresh.png"):
                        self.click("img/refresh.png")
                        print(f"[{self.device_id}] Clicked refresh.png")
                        time.sleep(3)
                        break
                    time.sleep(1)
                
                # 3) รอ check.png แล้วกด
                print(f"[{self.device_id}] Step 3: waiting for check.png (60s timeout)...")
                check_wait_start = time.time()
                while time.time() - check_wait_start < 60:
                    self.capture_screen()
                    if self.exists_in_cache("img/check.png"):
                        print(f"[{self.device_id}] Found check.png! Clicking...")
                        self.click("img/check.png")
                        time.sleep(2)
                        # หลังกด check -> รอดู fixid ก่อน 2 วิ
                        found_fixid_after_check = False
                        for _ in range(2):
                            self.capture_screen()
                            if self.exists_in_cache("img/fixid.png"):
                                print(f"[{self.device_id}] Found fixid.png right after check! Re-routing...")
                                found_fixid_after_check = True
                                break
                            time.sleep(1)
                        
                        if found_fixid_after_check:
                            break

                        if self.exists_in_cache("img/fikcheck.png", threshold=0.8):
                            print(f"[{self.device_id}] Found fikcheck.png after check! Clicking...")
                            self.click("img/fikcheck.png", threshold=0.8)
                            time.sleep(1)
                        break
                    time.sleep(1)
                
                continue

            # === เจอ refresh.png (ไม่มี fixid) -> กด refresh -> check ===
            if self.exists_in_cache("img/refresh.png"):
                print(f"[{self.device_id}] Found refresh.png (no fixid), clicking refresh -> check...")
                self.click("img/refresh.png")
                time.sleep(3)
                
                check_wait_start = time.time()
                while time.time() - check_wait_start < 60:
                    self.capture_screen()
                    if self.exists_in_cache("img/check.png"):
                        print(f"[{self.device_id}] Found check.png! Clicking...")
                        self.click("img/check.png")
                        time.sleep(2)
                        # หลังกด check -> รอดู fixid ก่อน 2 วิ
                        found_fixid_after_check = False
                        for _ in range(2):
                            self.capture_screen()
                            if self.exists_in_cache("img/fixid.png"):
                                print(f"[{self.device_id}] Found fixid.png right after check! Re-routing...")
                                found_fixid_after_check = True
                                break
                            time.sleep(1)
                        
                        if found_fixid_after_check:
                            break
                        
                        # หลังกด check -> หา fixok ด้วย
                        self.capture_screen()
                        if self.exists_in_cache("img/fixok.png"):
                            print(f"[{self.device_id}] Found fixok.png after check! Clicking...")
                            self.click("img/fixok.png")
                            time.sleep(1)
                        break
                    time.sleep(1)
                
                continue

            # Handle event sequence if found
            pos_ev = self.find_image("img/event.png", threshold=0.8)
            if pos_ev:
                print(f"[{self.device_id}] Found event.png, handling...")
                self.tap(pos_ev[0], pos_ev[1])
                time.sleep(1)
                back_count = 0
                while not self.find_image("img/cancel.png") and back_count < 10:
                    self.press_back()
                    time.sleep(1)
                    self.capture_screen()
                    if self.find_image("img/stoplogin.png"): break
                    back_count += 1
            
            # Common popups
            for img in ["alert2.png", "fixid.png", "fixok.png", "fixid1.png", "fixokk.png"]:
                pos = self.find_image(f"img/{img}")
                if pos:
                    self.tap(pos[0], pos[1], label=img)
                    time.sleep(1)

            if time.time() - start_time > 480: # 8 min timeout
                print(f"[{self.device_id}] Main login timeout.")
                return False
            time.sleep(1.5)
        return True

    def handle_login_failure(self):
        """Handle login failure by moving account to login-failed/ folder"""
        dst_dir = "login-failed"
        if not os.path.exists(dst_dir): os.makedirs(dst_dir)
        fname = self.current_account or "failed_unknown.xml"
        dst_path = os.path.join(dst_dir, fname)
        
        print(f"[{self.device_id}] FAILED LOGIN! Salvaging session to {dst_path}...")
        self.pull_file(dst_path)
        
        # Clear app immediately
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
        time.sleep(2)
        return False

    def capture_screen(self):
        # Regularly check if game process is alive
        self.check_pid_timer += 1
        if self.check_pid_timer >= 5:
            self.check_pid_timer = 0
            try:
                p_res = subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "pidof", "com.linecorp.LGRGS"], capture_output=True, timeout=5, **self.kwargs)
                if not p_res.stdout.strip():
                    print(f"[{self.device_id}] !!! GAME CRASHED (PID MISSING) !!! Triggering Recovery...")
                    raise GameCrashed()
            except GameCrashed:
                raise # Re-raise to be caught by run_step1
            except Exception:
                pass # ADB or pidof error, ignore for now

        # Popup check to handle common interruptions
        if not getattr(self, "_in_popup_check", False):
            self._in_popup_check = True
            try:
                self.check_floating_popups()
            except Exception as e:
                print(f"[{self.device_id}] Popup check error: {e}")
            finally:
                self._in_popup_check = False

        try:
            result = subprocess.run(
                [self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=15, **self.kwargs
            )
            if result.returncode == 0 and len(result.stdout) > 100:
                img_array = np.frombuffer(result.stdout, np.uint8)
                self.screen_bgr = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                self.screen_gray = cv2.cvtColor(self.screen_bgr, cv2.COLOR_BGR2GRAY)
                return True
        except Exception as e:
            print(f"[{self.device_id}] Capture error: {e}")
        return False

    def tap(self, x, y, label=None):
        if label:
            print(f"[{self.device_id}] Tapping {label} at ({x}, {y})")
        else:
            print(f"[{self.device_id}] Tapping screen...")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "tap", str(x), str(y)], **self.kwargs)

    def press_back(self):
        print(f"[{self.device_id}] Pressing BACK (ADB shell KEYCODE_BACK)")
        subprocess.run(
            [self.adb_cmd, "-s", self.device_id, "shell", "input", "keyevent", "KEYCODE_BACK"],
            **self.kwargs
        )

    def swipe(self, x1, y1, x2, y2, duration=1000):
        print(f"[{self.device_id}] Dragging from ({x1}, {y1}) to ({x2}, {y2}) over {duration}ms...")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)], **self.kwargs)

    def find_image(self, template_path, threshold=0.8):
        if self.screen_bgr is None: return None
        if not os.path.exists(template_path): return None
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template is None: return None
        res = cv2.matchTemplate(self.screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= threshold)
        if len(loc[0]) > 0:
            h, w, _ = template.shape
            return (int(loc[1][0] + w/2), int(loc[0][0] + h/2))
        return None

    def find_image_in_region(self, template_path, region, threshold=0.8):
        if self.screen_bgr is None: return None
        if not os.path.exists(template_path): return None
        rx, ry, rw, rh = region
        region_bgr = self.screen_bgr[ry:ry+rh, rx:rx+rw]
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template is None: return None
        res = cv2.matchTemplate(region_bgr, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= threshold)
        if len(loc[0]) > 0:
            h, w, _ = template.shape
            return (int(rx + loc[1][0] + w/2), int(ry + loc[0][0] + h/2))
        return None

    def wait_and_click(self, img_path, timeout=30, threshold=0.8):
        img_name = os.path.basename(img_path)
        start = time.time()
        last_log = time.time()
        while time.time() - start < timeout:
            self.capture_screen()

            pos = self.find_image(img_path, threshold)
            if pos:
                self.tap(pos[0], pos[1], label=img_name)
                return pos

            if time.time() - last_log > 5:
                rem = int(timeout - (time.time() - start))
                print(f"[{self.device_id}]   ...searching for {img_name} ({rem}s remaining of {timeout}s)")
                last_log = time.time()

            time.sleep(1)
        return None

    def exists_in_cache(self, template_path, threshold=0.8):
        """Check if image exists in the current screen cache"""
        if self.screen_bgr is None: return False
        if not os.path.exists(template_path): return False
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template is None: return False
        res = cv2.matchTemplate(self.screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= threshold)
        return len(loc[0]) > 0

    def click(self, template_path, threshold=0.8):
        """Find image in current cache and tap it"""
        pos = self.find_image(template_path, threshold)
        if pos:
            self.tap(pos[0], pos[1], label=os.path.basename(template_path))
            return True
        return False

    def _raw_capture(self):
        """Internal capture that bypasses additional hooks to prevent recursion"""
        try:
            result = subprocess.run(
                [self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=15, **self.kwargs
            )
            if result.returncode == 0 and len(result.stdout) > 100:
                img_array = np.frombuffer(result.stdout, np.uint8)
                self.screen_bgr = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                if self.screen_bgr is not None:
                    self.screen_gray = cv2.cvtColor(self.screen_bgr, cv2.COLOR_BGR2GRAY)
                return True
        except Exception as e:
            print(f"[{self.device_id}] Raw capture error: {e}")
        return False

    def check_floating_popups(self):
        """
        Check and click floating popups (checkline / fixnetv2 / fixplay / fixnet1 / fixnetv3).
        Loops until no popups are found.
        """
        max_popups_iterations = 5
        for _ in range(max_popups_iterations):
            found_any = False
            
            # checkline.png: Handle Checkbox Popup Sequence
            if self.exists_in_cache("img/checkline.png"):
                print(f"[{self.device_id}] [POPUP] checkline.png detected! Running special sequence...")
                self.click("img/checkline.png")
                time.sleep(2)
                found_any = True
                
                # Wait for check-l1.png (Wait up to 60s)
                start_l1 = time.time()
                while time.time() - start_l1 < 60:
                    self._raw_capture()
                    if self.exists_in_cache("img/check-l1.png"):
                        print(f"[{self.device_id}] [POPUP] Found check-l1.png")
                        break
                    time.sleep(1)
                
                # Coordinate taps based on newer system
                print(f"[{self.device_id}] [POPUP] Tapping checkbox coordinates (932,133), (930,253), (926,327)...")
                self.tap(932, 133); time.sleep(5)
                self.tap(930, 253); time.sleep(5)
                self.tap(926, 327); time.sleep(5)
                
                # Wait for check-l4.png
                start_l4 = time.time()
                while time.time() - start_l4 < 60:
                    self._raw_capture()
                    if self.exists_in_cache("img/check-l4.png"):
                        self.click("img/check-l4.png")
                        time.sleep(2); break
                    time.sleep(1)
                
                # Final OK
                print(f"[{self.device_id}] [POPUP] Waiting for check-ok1.png...")
                for _ in range(60):
                    self._raw_capture()
                    if self.exists_in_cache("img/check-ok1.png"):
                        self.click("img/check-ok1.png")
                        print(f"[{self.device_id}] [POPUP] checkline done.")
                        time.sleep(2)
                        break
                    time.sleep(1)

            # fixnetv2.png: Network/Retry Sequence
            if self.exists_in_cache("img/fixnetv2.png"):
                print(f"[{self.device_id}] [POPUP] fixnetv2.png detected! Executing retry clicks...")
                self.click("img/fixnetv2.png")
                time.sleep(3)
                self.click("img/fixnetv2ok.png")
                time.sleep(2)
                found_any = True

            # fixplay.png: Google Play Popup
            if self.exists_in_cache("img/fixplay.png"):
                print(f"[{self.device_id}] [POPUP] fixplay.png detected! Clicking OK...")
                self.click("img/ok.png")
                time.sleep(2)
                found_any = True

            # fixnet.png: General Connection Error
            if self.exists_in_cache("img/fixnet.png"):
                print(f"[{self.device_id}] [POPUP] fixnet.png detected! Clicking OK Reset...")
                self.click("img/oknet.png")
                time.sleep(2)
                found_any = True

            # fixnet1.png: Secondary Connection Error
            if self.exists_in_cache("img/fixnet1.png"):
                print(f"[{self.device_id}] [POPUP] fixnet1.png detected! Tapping (476, 394)...")
                self.tap(476, 394)
                time.sleep(2)
                found_any = True

            # fixnetv3.png: Force Restart Loop
            if self.exists_in_cache("img/fixnetv3.png"):
                print(f"[{self.device_id}] [POPUP] fixnetv3.png detected! Incrementing restart count...")
                self._fixnetv3_count += 1
                if self._fixnetv3_count >= 3:
                    print(f"[{self.device_id}] [RESTART] Hit 3x fixnetv3.png! Forcing bot restart...")
                    self._need_restart = True
                    self._fixnetv3_count = 0
                self.click("img/fixnetv3.png")
                time.sleep(2)
                found_any = True

            if not found_any:
                # --- CHECK LV LOGIC ---
                skip_lv = self.config.get("skip-lv", 0)
                if skip_lv == 1 and not getattr(self, "_checklv_done", False):
                    if self.exists_in_cache("img/checkpont-lv.png"):
                        print(f"[{self.device_id}] [CHECK-LV] Target found! Verifying level...")
                        self._checklv_done = True
                        
                        # OCR Region: 25, 17, 81, 74
                        region = (25, 17, 81, 74)
                        rx, ry, rw, rh = region
                        img_crop = self.screen_bgr[ry:ry+rh, rx:rx+rw]
                        processed_img = cv2.resize(img_crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                        
                        import easyocr
                        if self._ocr_reader is None:
                            self._ocr_reader = easyocr.Reader(['en'], gpu=False)
                            
                        results = self._ocr_reader.readtext(processed_img, allowlist='0123456789')
                        found_lv = None
                        for (bbox, text, conf) in results:
                            if conf > 0.2:
                                digits = re.findall(r'\d+', text)
                                if digits:
                                    found_lv = int(digits[0])
                                    break
                        
                        if found_lv is not None:
                            print(f"[{self.device_id}] [CHECK-LV] Detected Level: {found_lv}")
                            if found_lv > 4:
                                print(f"[{self.device_id}] [CHECK-LV] Level {found_lv} > 4! Moving account to lv5+ and stopping.")
                                # Move file
                                if self.current_account:
                                    source = os.path.join("backup", self.current_account)
                                    dest_dir = "lv5+"
                                    if not os.path.exists(dest_dir): os.makedirs(dest_dir)
                                    dest = os.path.join(dest_dir, self.current_account)
                                    try:
                                        shutil.move(source, dest)
                                        print(f"[{self.device_id}] Account moved to {dest}")
                                    except Exception as e:
                                        print(f"[{self.device_id}] Error moving account: {e}")
                                
                                # Force Stop and Exit
                                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
                                raise AccountFinished()
                            else:
                                print(f"[{self.device_id}] [CHECK-LV] Level {found_lv} <= 4. Continuing normally.")
                        else:
                            print(f"[{self.device_id}] [CHECK-LV] Could not read level. Skipping for now.")
                            self._checklv_done = False # Try again later if still on screen

                break
            else:
                self._raw_capture() # Update cache for next iteration

    def handle_quest_151(self):
        """
        Stage 151 reached! Force stop app, pull account XML,
        save as stage151_OriginalName.xml in backup-id/, then signal finish.
        """
        print(f"[{self.device_id}] >>> STAGE 151 QUEST ROUTINE STARTED <<<")
        
        # 1. Force Stop Game
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
        time.sleep(2)
        
        # 2. Prepare Destination
        backup_id_dir = "backup-id"
        if not os.path.exists(backup_id_dir):
            os.makedirs(backup_id_dir)
            
        orig_filename = self.current_account if self.current_account else "unknown.xml"
        dest_filename = f"stage151_{orig_filename}"
        dest_path = os.path.join(backup_id_dir, dest_filename)
        
        # 3. Pull File
        remote_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        print(f"[{self.device_id}] Backing up account to {dest_path}...")
        # Fix permissions before pulling
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", f"chmod 666 {remote_path}"], **self.kwargs)
        subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", remote_path, dest_path], **self.kwargs)
        
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 100:
            print(f"[{self.device_id}] Backup successful: {dest_filename}")
            return False # Return False to signal AccountFinished (Stop current work on this ID)
        else:
            print(f"[{self.device_id}] ERROR: Backup failed / File missing!")
            return False # Still return False to stop trying this account as it might be stuck

    def _get_lock_path(self, xml_file):
        """Get lock file path in temp directory"""
        lock_dir = os.path.join(tempfile.gettempdir(), "ranger-locks")
        if not os.path.exists(lock_dir):
            os.makedirs(lock_dir, exist_ok=True)
        lock_name = os.path.basename(xml_file) + ".lock"
        return os.path.join(lock_dir, lock_name)

    def _get_next_available_file(self):
        """Finds next .xml file in backup/ and attempts to lock it atomically."""
        source_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
        if not os.path.exists(source_folder): return None
        
        files = [os.path.join(source_folder, f) for f in os.listdir(source_folder) if f.lower().endswith(".xml") and not f.startswith("stage151_")]
        random.shuffle(files)
        
        for xml_file in files:
            lock_file = self._get_lock_path(xml_file)
            
            # Clean stale locks (> 30 mins)
            if os.path.exists(lock_file):
                if time.time() - os.path.getmtime(lock_file) > 1800:
                    try: os.remove(lock_file)
                    except: pass
                else: continue
            
            # Atomic Lock
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w') as f:
                    f.write(self.device_id)
                return os.path.basename(xml_file)
            except FileExistsError:
                continue
            except Exception:
                continue
        return None

    def _release_file_lock(self, fname):
        if not fname: return
        xml_file = os.path.join("backup", fname)
        lock_file = self._get_lock_path(xml_file)
        if os.path.exists(lock_file):
            try: os.remove(lock_file)
            except: pass

    def wait_for_image(self, img_path, timeout=30, threshold=0.8):
        start = time.time()
        while time.time() - start < timeout:
            self.capture_screen()
            if self.find_image(img_path, threshold):
                return True
            time.sleep(1)
        return False

    def wait_and_ocr_click(self, text_list, region=None, timeout=30):
        import re
        if isinstance(text_list, str):
            text_list = [text_list]

        start_time = time.time()
        is_infinite = timeout is None or timeout == 0

        while is_infinite or (time.time() - start_time < timeout):
            self.capture_screen()
            if self.screen_bgr is None:
                time.sleep(1)
                continue

            try:
                import easyocr
                if self._ocr_reader is None:
                    print(f"[{self.device_id}] Initializing EasyOCR...")
                    self._ocr_reader = easyocr.Reader(['en'], gpu=False)

                if region:
                    rx, ry, rw, rh = region
                    img_crop = self.screen_bgr[ry:ry+rh, rx:rx+rw]
                    processed_img = cv2.resize(img_crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                results = self._ocr_reader.readtext(processed_img, allowlist='0123456789sS')

                for (bbox, text, conf) in results:
                    numbers_found = re.findall(r'\d+', text)

                    if conf > 0.2:
                        print(f"[{self.device_id}] OCR Read: '{text}' (conf: {conf:.2f}) (Digits: {numbers_found})")

                    for target_text in text_list:
                        if target_text.isdigit():
                            if target_text in numbers_found:
                                (tl, tr, br, bl) = bbox
                                center_x = int((tl[0] + br[0]) / 2 / 2) + rx
                                center_y = int((tl[1] + br[1]) / 2 / 2) + ry

                                print(f"[{self.device_id}] => Found '{target_text}', clicking at ({center_x}, {center_y})...")
                                self.tap(center_x, center_y)
                                return True
            except Exception as e:
                print(f"[{self.device_id}] OCR Error: {e}")

            time.sleep(1)
        return False

    def advanced_drag_hold(self, points, hold_sec=3):
        p1, p2, p3 = points
        print(f"[{self.device_id}]   [Double-Drag Hold] Phase 1: {p1}->{p2} (Release)")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                        str(p1[0]), str(p1[1]), str(p2[0]), str(p2[1]), "500"], **self.kwargs)
        time.sleep(1.0)
        print(f"[{self.device_id}]   [Double-Drag Hold] Phase 2: DragAndDrop {p2} -> {p3} (Focusing Hold)")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "draganddrop",
                        str(p2[0]), str(p2[1]), str(p3[0]), str(p3[1]), str(hold_sec*1000)], **self.kwargs)

    def handle_clear_routine(self, stage_num):
        """Standardized Master Clear Routine (Deep Reward Edition)"""
        print(f"[{self.device_id}]   Starting clear sequence (Stage {stage_num})...")

        def wc(img, timeout=3): return self.wait_and_click(img, timeout=timeout)
        
        def reward_sweep(label="Reward Sweep", timeout_idle=10):
            print(f"[{self.device_id}]   {label}: Searching for all reward items...")
            reward_items = [
                "clear1.png", "clear2.png", "clear3.png", "clearok.png",
                "itemstage.png", "okpuzzle.png", "ozpuzzle.png", "clear lv.png", "clearstop.png"
            ]
            last_activity = time.time()
            while time.time() - last_activity < timeout_idle:
                self.capture_screen()
                found_any = False
                for img_name in reward_items:
                    pos = self.find_image(f"img/{img_name}", threshold=0.8)
                    if pos:
                        self.tap(pos[0], pos[1], label=f"reward-{img_name}")
                        last_activity = time.time()
                        found_any = True
                        time.sleep(0.4)
                        break 
                if not found_any: time.sleep(0.5)
            print(f"[{self.device_id}]   {label} finished.")

        # ============================================================
        # STAGE 5 (High Priority - Exact Sequential Logic)
        # ============================================================
        if stage_num == 5:
            print(f"[{self.device_id}] Stage 5: Starting 100% Exact Sequential Logic...")
            
            # 1. clear1-> clear2 -> skip -> skipok
            wc("img/clear1.png", 15); wc("img/clear2.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # 2. itemstage.png -> okpuzzle
            wc("img/itemstage.png", 15); wc("img/okpuzzle.png", 15)
            
            # 3. clear2 (Find 2 REPS) -> clear3 (Find 2 REPS)
            for _ in range(2): wc("img/clear2.png", 15)
            for _ in range(2): wc("img/clear3.png", 15)
            
            # 4. eventstage5 10s -> skip 10s -> skipok 10s -> mainstage
            wc("img/eventstage5.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15); wc("img/mainstage.png", 15)
            
            # 5. chest1 80s -> skip -> skipok
            wc("img/chest1.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # 6. Loop eventstage5: Wait 10s, if found click, repeat until not found for 10s
            print(f"[{self.device_id}]   Stage 5: Checking for eventstage5.png (10s window)...")
            while True:
                if wc("img/eventstage5.png", 15):
                    time.sleep(1)
                else: 
                    break
            
            # 7. skip (search indefinitely until found) -> skipok 30s -> skip 30s -> skipok 30s
            print(f"[{self.device_id}]   Stage 5: Waiting indefinitely for skip.png...")
            while not wc("img/skip.png", 15): pass
            wc("img/skipok.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # 8. quest -> quest1 -> quest2 -> quest3
            wc("img/quest.png", 15); wc("img/quest1.png", 15); wc("img/quest2.png", 15); wc("img/quest3.png", 15)
            
            # 9. mainstage -> wait for waitmainstage.png -> KEYCODE_BACK
            wc("img/mainstage.png", 15)
            if self.wait_for_image("img/waitmainstage.png", timeout=60):
                print(f"[{self.device_id}]   Found waitmainstage, sending Keycode Back...")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "keyevent", "KEYCODE_BACK"], **self.kwargs)
                time.sleep(1)
            
            # 10. Final eventstage5 check (10s) -> mainstage
            wc("img/eventstage5.png", 15); wc("img/mainstage.png", 15)
            
            print(f"[{self.device_id}] Stage 5 Sequence 100% Completed.")
            return True

        # ============================================================
        # STAGE 12 (Strict Exact Logic - WITH INITIAL SWEEP)
        # ============================================================
        if stage_num == 12:
            print(f"[{self.device_id}] Stage 12: First Full Clearing...")
            reward_sweep("Initial Sweep")
            print(f"[{self.device_id}] Stage 12: Starting Exact Sequential Logic...")
            
            # -> eventstage11 30s -> skip -> skipok
            wc("img/eventstage11.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # -> gacha1 (loop until gone) -> gacha2 30s -> skip -> skipok
            self.process_sequence([{"img": "img/gacha1.png", "loop": True, "timeout": 15}])
            wc("img/gacha2.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # -> gear1 -> gear2 -> gear3 -> gear4 -> gear5 -> skip -> skipok
            for i in [1, 2, 3, 4, 5]:
                wc(f"img/gear{i}.png", 15)
            wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # -> gearep1 -> skip -> skipok -> skip 30s
            wc("img/gearep1.png", 15)
            wc("img/skip.png", 15); wc("img/skipok.png", 15); wc("img/skip.png", 15)
            
            # -> gearep2 -> skip -> skipok
            wc("img/gearep2.png", 15)
            wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # -> gearep3 -> กดตำแหน่งเดิม5รอบ
            p_ep3 = wc("img/gearep3.png", 15)
            if p_ep3:
                for _ in range(5): self.tap(p_ep3[0], p_ep3[1], label="gearep3-repeat"); time.sleep(0.3)
            
            # -> gearep4 -> skip -> skip 30s
            wc("img/gearep4.png", 15)
            wc("img/skip.png", 15); wc("img/skip.png", 15)
            
            # -> backgearep1 -> mainstage
            wc("img/backgearep1.png", 15)
            wc("img/mainstage.png", 15)
            
            print(f"[{self.device_id}] Stage 12 Sequence 100% Completed.")
            return True

        # ============================================================
        # STAGE 15 (Exact Sequential Logic + Initial Sweep)
        # ============================================================
        if stage_num == 15:
            print(f"[{self.device_id}] Stage 15: First Full Clearing...")
            reward_sweep("Initial Sweep")
            print(f"[{self.device_id}] Stage 15: Starting Exact Sequential Logic...")
            wc("img/skip.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15)
            # Loop egear1 -> 5 indefinitely until found, then tap until gone
            last_eg5_pos = None
            for img_name in ["egear1.png", "egear2.png", "egear3.png", "egear4.png", "egear5.png"]:
                print(f"[{self.device_id}]   Waiting indefinitely for {img_name}...")
                while True:
                    self.capture_screen()
                    p = self.find_image(f"img/{img_name}", 0.8)
                    if p: break
                    time.sleep(0.5)

                print(f"[{self.device_id}]   Found {img_name}, looping until gone...")
                while True:
                    self.capture_screen()
                    p = self.find_image(f"img/{img_name}", 0.8)
                    if p:
                        self.tap(p[0], p[1], label=f"loop-{img_name}")
                        if img_name == "egear5.png": last_eg5_pos = p
                        time.sleep(0.5); continue
                    break
            # 8 extra taps on egear5 position
            if last_eg5_pos:
                print(f"[{self.device_id}]   Delaying 2s before eg5 extra taps...")
                time.sleep(2)
                for _ in range(8): 
                    self.tap(last_eg5_pos[0], last_eg5_pos[1], label="eg5-repeat")
                    time.sleep(2.0)
            
            wc("img/skip.png", 15); wc("img/skipok.png", 15)

            # Loop backegear until gone
            print(f"[{self.device_id}]   Looping backegear.png until gone...")
            while True:
                self.capture_screen()
                pb = self.find_image("img/backegear.png", 0.8)
                if pb: self.tap(pb[0], pb[1], label="backegear"); time.sleep(0.5); continue
                break
            wc("img/mainstage.png")
            return True

        # ============================================================
        # INITIAL REWARD COLLECTION (For all other NORMAL stages)
        # ============================================================
        if stage_num == 27:
            print(f"[{self.device_id}] Stage 27 special: Waiting 15s before starting clear sweep...")
            time.sleep(15)
            
        reward_sweep("Initial Sweep")

        if stage_num == 10:
            print(f"[{self.device_id}] Stage 10: Starting update and finishing flow...")
            wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            while True:
                self.capture_screen()
                u1 = self.find_image("img/update1.png", 0.8); u2 = self.find_image("img/update2.png", 0.8)
                if u1: self.tap(u1[0], u1[1], label="update1"); continue
                if u2: self.tap(u2[0], u2[1], label="update2"); continue
                pos_drag = self.find_image("img/drag1.png", threshold=0.85)
                if pos_drag:
                    print(f"[{self.device_id}]   Found drag1 at {pos_drag}, Sweeping from 99, 447 to 395, 259 (800ms)...")
                    subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", "99", "447", "395", "259", "800"], **self.kwargs)
                    time.sleep(1); break
                time.sleep(1)
            
            # Click update3 and repeat slowly (6 reps with 2s delay)
            p_u3 = self.wait_and_click("img/update3.png", timeout=20)
            if p_u3:
                print(f"[{self.device_id}] Clicking update3 repeats (Slower: 2s delay)...")
                for _ in range(5): 
                    time.sleep(2.0)
                    self.tap(p_u3[0], p_u3[1], label="update3-repeat")
            
            # Finishing Flow (Ensuring it gets back to map for Stage 11)
            print(f"[{self.device_id}] Stage 10: Executing finishing sequence...")
            wc("img/skip.png", 15); wc("img/skipok.png", 15)
            wc("img/updateback.png", 15); wc("img/mainstage.png", 15)
            
            reward_sweep("Stage 10 Cleanup", timeout_idle=5)
            return True

        # ============================================================
        # DEFAULT WRAP-UP
        # ============================================================
        wc("img/skip.png", 15); wc("img/skipok.png", 15); wc("img/mainstage.png", 15)
        reward_sweep("Default Final Cleanup", timeout_idle=5)
        return True



    def process_sequence(self, sequence_def):
        """Processes a sequence of images, waiting and (optionally) loop-clicking them."""
        for item in sequence_def:
            img_path = item["img"]
            img_name = os.path.basename(img_path)
            is_loop = item.get("loop", True)
            to = item.get("timeout", None)
            is_critical = item.get("critical", False)
            max_clicks = item.get("max_clicks", 5)

            print(f"[{self.device_id}]   SeqWait for {img_name}... (Timeout: {to or 'Infinite'})")
            start_wait = time.time()
            found = False
            while True:
                if to is not None and time.time() - start_wait > to:
                    break
                self.capture_screen()

                if self.find_image(img_path, 0.8):
                    found = True
                    break
                time.sleep(0.5)
            
            if not found:
                print(f"[{self.device_id}]     {img_name} not found, skipping.")
                if is_critical:
                    return False
                continue
            
            print(f"[{self.device_id}]     Found {img_name}! Executing '{'loop-click' if is_loop else 'single-click'}'...")
            clicks = 0
            while True:
                self.capture_screen()
                pos = self.find_image(img_path, 0.8)
                if not pos:
                    break # Success! Image is gone
                
                self.tap(pos[0], pos[1], label=img_name)
                clicks += 1
                
                if max_clicks and clicks >= max_clicks:
                    print(f"[{self.device_id}]     Max clicks ({max_clicks}) reached for {img_name} ({clicks}/{max_clicks}).")
                    break
                
                if not is_loop:
                    break
                
                time.sleep(0.6) # Short wait before next click to let game update state
        return True

    def handle_quest_151(self):
        self.in_quest_routine = True
        try:
            self.load_config()
            g_quest = self.config.get("getclearquest", 0)

            if g_quest == 0:
                print(f"[{self.device_id}] >>> STAGE 151 DETECTED: AUTO-BACKUP & SWITCH (getclearquest=0) <<<")
                
                # 1. Clear Game
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
                time.sleep(2)

                # 2. Backup File
                backup_dir = "backup-id"
                if not os.path.exists(backup_dir): os.makedirs(backup_dir)
                orig_name = self.current_account or "unknown.xml"
                dest_path = os.path.join(backup_dir, f"stage151_{orig_name}")
                
                print(f"[{self.device_id}] Backup Mode (getclearquest: 0) -> Pulling Account to {dest_path}...")
                self.pull_file(dest_path)
                print(f"[{self.device_id}] Account saved. Switching to next ID.")
                return False # Signal to break per-account loop
            
            else:
                print(f"[{self.device_id}] >>> STAGE 151 QUEST ROUTINE STARTED (getclearquest=1) <<<")
                
                # Initial setup (Using timeout=99999 from user snippet)
                self.wait_and_click("img/quest-stage1.png", timeout=99999)
                self.wait_and_click("img/getquse1.png", timeout=99999)
                self.wait_and_click("img/okquest.png", timeout=99999)

                # Play 1 Quest
                print(f"[{self.device_id}]   Processing Play 1 Quest...")
                self.wait_and_click("img/goquse1.png", timeout=99999)
                pos_pq1 = self.wait_and_click("img/playques1.png", timeout=99999)
                if pos_pq1:
                    self.tap(pos_pq1[0], pos_pq1[1], label="playques1_rep")
                
                print(f"[{self.device_id}]   Delaying 3s then tapping coord 125,29 & 393,449...")
                time.sleep(3)
                self.tap(125, 29, label="coord_125_29")
                time.sleep(1)
                self.tap(393, 449, label="coord_393_449")
                time.sleep(1)

                for q_img in ["img/playquest2.png", "img/playquest3.png", "img/playquest4.png"]:
                    self.wait_and_click(q_img, timeout=99999)

                print(f"[{self.device_id}]   Executing quest swipe...")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", "99", "447", "395", "259", "800"], **self.kwargs)
                time.sleep(1)

                self.wait_and_click("img/playquest5.png", timeout=99999)
                self.wait_and_click("img/playquest6.png", timeout=99999)

                # Play 2 Quest
                print(f"[{self.device_id}]   Processing Play 2 Quest sequence...")
                print(f"[{self.device_id}]   Tapping coordinate 282, 196 twice...")
                for _ in range(2):
                    self.tap(282, 196, label="coord_282_196")
                    time.sleep(0.5)

                self.wait_and_click("img/back-mainstage.png", timeout=99999)
                self.wait_and_click("img/back-mainstage1.png", timeout=99999)
                self.wait_and_click("img/quest-stage1.png", timeout=99999)
                self.wait_and_click("img/getquse1.png", timeout=99999)
                self.wait_and_click("img/okquest.png", timeout=99999)
                self.wait_and_click("img/goquse1.png", timeout=99999)
                
                print(f"[{self.device_id}] >>> STAGE 151 QUEST ROUTINE FINISHED <<<")
                return True
        finally:
            self.in_quest_routine = False

    def handle_battle_31(self):
        print(f"[{self.device_id}] >>> Phase 2: Battle 31+ System...")
        
        # 1. Burst start
        hero_coords = [(278, 521), (384, 514), (483, 508), (582, 515), (683, 517), (146, 483)]
        for _ in range(6):
            tap_chain = " & ".join([f"input tap {c[0]} {c[1]}" for c in hero_coords]) + " & wait"
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", tap_chain], **self.kwargs)
            time.sleep(0.1)

        # 2. Priority Position 7
        print(f"[{self.device_id}]   Priority Position 7 (50, 43) x5 FIRST")
        for _ in range(5):
            self.tap(50, 43, label="priority_pos7")
            time.sleep(0.1)

        stop_spam = False
        def spam_heroes_31():
            burst = [(50, 43), (50, 43), (50, 43)] + hero_coords
            tap_chain = " & ".join([f"input tap {c[0]} {c[1]}" for c in burst]) + " & wait"
            while not stop_spam:
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", tap_chain], **self.kwargs)
                time.sleep(0.01)

        spam_thread = Thread(target=spam_heroes_31)
        spam_thread.start()

        win_detected = False
        while not win_detected:
            self.capture_screen()
            
            # Check for stage151 interrupt
            if self.find_image("img/stage151.png", threshold=0.95):
                print(f"[{self.device_id}] Interrupt: Stage 151 detected in battle!")
                stop_spam = True
                spam_thread.join()
                if not self.handle_quest_151():
                    return False, "stop" # Signal stop work
                return False, "interrupted"

            # Use items
            for i in range(1, 5):
                pos = self.find_image(f"img/useitem{i}.png")
                if pos:
                    self.tap(pos[0], pos[1])
                    time.sleep(1)

            self.capture_screen()
            if self.find_image("img/win.png"):
                print(f"[{self.device_id}] WIN detected!")
                stop_spam = True
                win_detected = True
                break
            time.sleep(0.5)
            
        spam_thread.join()
        return True, "win"

    def handle_finish_31(self):
        print(f"[{self.device_id}] >>> Phase 3: Finish & Rewards (Priority Loop)...")
        
        start_time = time.time()
        while time.time() - start_time < 60:
            self.capture_screen()
            
            if self.find_image("img/stage151.png", threshold=0.95):
                if not self.handle_quest_151():
                    return "stop"
                return "interrupted"

            # Check for floating NextStage (Orange)
            pos_ns = self.find_image("img/nextstage.png", threshold=0.8)
            if pos_ns:
                time.sleep(0.5)
                self.tap(pos_ns[0], pos_ns[1], label="nextstage_repeat")
                print(f"[{self.device_id}]   !!! FLOATING NEXTSTAGE DETECTED !!! Tapping reward pos 218, 46 x6...")
                for _ in range(6):
                    time.sleep(2.0)
                    self.tap(218, 46)
                print(f"[{self.device_id}]   Bypassing the rest of the clear sequence.")
                return "jump"

            # Check for NextNew
            pos_nn = self.find_image("img/nextnew.png", threshold=0.8)
            if pos_nn:
                print(f"[{self.device_id}]   !!! FLOATING NEXTNEW DETECTED !!! Tapping 162, 32 -> Jump.")
                self.tap(pos_nn[0], pos_nn[1])
                self.tap(162, 32)
                time.sleep(1)
                return "jump"

            reward_items = [
                "clear1.png", "clear2.png", "clear3.png", "clearok.png",
                "clear lv.png", "clearstop.png", "okpuzzle.png", "itemstage.png"
            ]
            found_any = False
            for img_name in reward_items:
                pos = self.find_image(f"img/{img_name}", threshold=0.8)
                if pos:
                    self.tap(pos[0], pos[1], label=f"reward-{img_name}")
                    found_any = True
                    time.sleep(0.4)
                    break
            
            if not found_any:
                if self.find_image("img/side.png") or self.find_image("img/buyhelp.png"):
                    print(f"[{self.device_id}]   Detected map elements. Finishing Phase 3.")
                    break
                time.sleep(0.5)
                
        return "normal"

    def find_team(self):
        REGION_SCAN = (165, 490, 70, 21)
        TARGET_VALUE = 1200
        HERO_SLOTS = [
            (622, 248),
            (768, 251),
            (447, 179),
            (339, 183),
            (196, 188),
        ]
        HERO_SLOTSCLear = [
            (767, 175),
            (624, 173),
            (447, 179),
            (339, 183),
            (196, 188),
        ]
        CLEAR_DROP = (258, 444)
        REST_POINT = (486, 189)

        class DeviceWrapper:
            def __init__(self, bot):
                self.bot = bot
                self.device_id = bot.device_id
            def capture_screen(self):
                self.bot.capture_screen()
                return self.bot.screen_bgr

        scanner = HeroScanner(DeviceWrapper(self))

        print(f"[{self.device_id}] ==================================================")
        print(f"[{self.device_id}]   FIND TEAM - START")
        print(f"[{self.device_id}] ==================================================")

        print(f"[{self.device_id}] STEP 1: Clicking [team]...")
        while not self.wait_and_click("img/team.png", timeout=60, threshold=0.8):
            time.sleep(1)

        time.sleep(1)

        print(f"[{self.device_id}] STEP 2: Waiting for [waitteam]...")
        while True:
            if self.wait_for_image("img/waitteam.png", timeout=60, threshold=0.8):
                print(f"[{self.device_id}] waitteam found! Proceeding...")
                break
            print(f"[{self.device_id}] waitteam not found yet, retrying...")
            time.sleep(1)
        time.sleep(1)

        print(f"[{self.device_id}] STEP 3: Clearing all hero slots...")
        for i, slot in enumerate(HERO_SLOTSCLear):
            print(f"[{self.device_id}]   Clearing hero{i+1}: ({slot[0]}, {slot[1]}) -> ({CLEAR_DROP[0]}, {CLEAR_DROP[1]})")
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                            str(slot[0]), str(slot[1]), str(CLEAR_DROP[0]), str(CLEAR_DROP[1]), "500"], **self.kwargs)
            time.sleep(0.3)
        time.sleep(0.5)

        print(f"[{self.device_id}] STEP 4: Applying filters...")
        filters = ["img/filter1.png", "img/filter2.png", "img/filter3.png", "img/filter4.png", "img/filter5.png"]
        for f_img in filters:
            f_name = os.path.basename(f_img)
            print(f"[{self.device_id}]   Clicking {f_name}...")
            if self.wait_and_click(f_img, timeout=15, threshold=0.95):
                time.sleep(0.3)
            else:
                print(f"[{self.device_id}]   WARNING: {f_name} not found, continuing anyway...")
        time.sleep(0.5)

        print(f"[{self.device_id}] STEP 5: Scanning and dragging heroes...")
        filled_count = 0
        scroll_count = 0
        max_scroll = 20
        dragged_values = []

        while filled_count < 5 and scroll_count <= max_scroll:
            print(f"[{self.device_id}]   Scanning region {REGION_SCAN}...")
            candidates = scanner.find_numbers_in_region(REGION_SCAN)
            under_target = [c for c in candidates if c["val"] < TARGET_VALUE and c["val"] >= 100 and c["conf"] >= 0.5]

            if under_target:
                hero_info = [f"{c['val']}(conf:{c['conf']:.2f})" for c in under_target]
                print(f"[{self.device_id}]   Found candidate(s): {hero_info}")

                for hero in under_target:
                    if filled_count >= 5: break

                    if hero["val"] in dragged_values:
                        print(f"[{self.device_id}]   Skipping hero {hero['val']} - Identical value already in team.")
                        continue

                    target_slot = HERO_SLOTS[filled_count]
                    sx, sy = hero["pos"]

                    print(f"[{self.device_id}]   Dragging hero {hero['val']} from ({sx}, {sy}) -> hero{filled_count+1} {target_slot}")

                    if target_slot in [(622, 248), (768, 251)]:
                        self.advanced_drag_hold([(sx, sy), REST_POINT, target_slot], hold_sec=3)
                    else:
                        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                                        str(sx), str(sy), str(target_slot[0]), str(target_slot[1]), "500"], **self.kwargs)

                    time.sleep(5)
                    dragged_values.append(hero["val"])
                    filled_count += 1
                    print(f"[{self.device_id}]   Slots filled: {filled_count}/5 (Team Values: {dragged_values})")

            if filled_count < 5:
                scroll_count += 1
                print(f"[{self.device_id}]   Scrolling to next candidate (attempt {scroll_count}/{max_scroll})...")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                                "254", "442", "193", "447", "1000"], **self.kwargs)
                time.sleep(3)
            else:
                print(f"[{self.device_id}] Find team complete, stopping scan.")
                break

        if filled_count >= 5:
            print(f"[{self.device_id}] All 5 hero slots filled!")
        else:
            print(f"[{self.device_id}] Could only fill {filled_count}/5 slots after {scroll_count} scrolls.")

        print(f"[{self.device_id}] STEP 6: Saving team...")
        time.sleep(1)
        print(f"[{self.device_id}]   Clicking [backhero]...")
        self.wait_and_click("img/backhero.png", timeout=15, threshold=0.8)
        time.sleep(1)
        print(f"[{self.device_id}]   Clicking [saveteam]...")
        self.wait_and_click("img/saveteam.png", timeout=15, threshold=0.8)
        time.sleep(1)
        print(f"[{self.device_id}] ==================================================")
        print(f"[{self.device_id}]   FIND TEAM - COMPLETED!")
        print(f"[{self.device_id}] ==================================================")

    def run_step1(self):
        while True:
            fname = self._get_next_available_file()
            if not fname:
                print(f"[{self.device_id}] No available accounts in queue. Waiting 10s...")
                time.sleep(10)
                continue
                
            self.current_account = fname
            self._checklv_done = False
            print(f"[{self.device_id}] >>> PROCESSING NEW ACCOUNT: {fname} <<<")
            
            while True: # RETRY LOOP (Current Account)
                try:
                    if self._need_restart:
                        print(f"[{self.device_id}] RECOVERY: Need restart flag set. Restarting account flow...")
                        self._need_restart = False
                        # Ensure app is closed
                        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
                        time.sleep(3)

                    self.load_config()
                    # 1. Clear App
                    subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
                    time.sleep(2)
                    
                    # 2. Push file
                    local_orig_path = os.path.join("backup", fname)
                    self.push_file(local_orig_path)
                    
                    # 3. Login routine
                    login_res = self.main_login()
                    if login_res == "restart":
                        self._need_restart = True
                        continue
                    if login_res == "failed" or login_res == False:
                        print(f"[{self.device_id}] !! Login failed for {fname}. Moving to next.")
                        self._release_file_lock(fname)
                        break
            
                    # --- MAIN PLAY SEQUENCE ---
                    self.find_team()

                    stage_sequence = [
                        {"num": 3,  "img": "img/stage/num3.png",       "region": (182, 244, 665, 180)},
                        {"num": 4,  "img": "img/stage/num4.png",       "region": (334, 227, 271, 113)},
                        {"num": 5,  "img": "img/stage/num5.png",       "region": (144, 179, 433, 277)},
                        {"num": 6,  "img": "img/stage/num6.png",       "region": (136, 92,  324, 316)},
                        {"num": 7,  "img": "img/stage/num7.png",       "region": (99,  188, 386, 176)},
                        {"num": 8,  "img": "img/stage/num8.png",       "region": (123, 199, 403, 162)},
                        {"num": 9,  "img": "img/stage/num9.png",       "region": (232, 207, 420, 166)},
                        {"num": 10, "img": "img/stage/num10.png",      "region": (400, 180, 200, 120)},
                        {"num": 11, "img": "img/stage/num11.png",      "region": (0, 0, 800, 600)},
                        {"num": 12, "img": "img/stage/stage boss.png", "region": (0, 0, 800, 600)},
                        {"num": 13, "img": "img/stage/num13.png",      "region": (0, 0, 800, 600)},
                        {"num": 14, "img": "img/stage/num14.png",      "region": (0, 0, 800, 600)},
                        {"num": 15, "img": "img/stage/num15.png",      "region": (0, 0, 800, 600)},
                        {"num": 16, "img": "img/stage/num16.png",      "region": (0, 0, 800, 600)},
                        {"num": 17, "img": "img/stage/num17.png",      "region": (0, 0, 800, 600)},
                        {"num": 18, "img": "img/stage/num18.png",      "region": (0, 0, 800, 600)},
                        {"num": 19, "img": "img/stage/num19.png",      "region": (0, 0, 800, 600)},
                        {"num": 20, "img": "img/stage/num20.png",      "region": (0, 0, 800, 600)},
                        {"num": 21, "img": "img/stage/num21.png",      "region": (0, 0, 800, 600)},
                        {"num": 22, "img": "img/stage/num22.png",      "region": (0, 0, 800, 600)},
                        {"num": 23, "img": "img/stage/num23.png",      "region": (0, 0, 800, 600)},
                        {"num": 24, "img": "img/stage/stage boss.png", "region": (0, 0, 800, 600)},
                        {"num": 25, "img": "img/stage/num25.png",      "region": (0, 0, 800, 600)},
                        {"num": 26, "img": "img/stage/num26.png",      "region": (0, 0, 800, 600)},
                        {"num": 27, "img": "img/stage/num27.png",      "region": (0, 0, 800, 600)},
                        {"num": 28, "img": "img/stage/num28.png",      "region": (0, 0, 800, 600)},
                        {"num": 29, "img": "img/stage/num29.png",      "region": (0, 0, 800, 600)},
                        {"num": 30, "img": "img/stage/num30.png",      "region": (0, 0, 800, 600)},
                        {"num": 31, "img": "img/stage/num31.png",      "region": (0, 0, 800, 600)},
                    ]

                    for stage_info in stage_sequence:
                        target_img = stage_info["img"]
                        region     = stage_info["region"]
                        stage_num  = stage_info["num"]

                        print(f"[{self.device_id}] === TARGET STAGE: {stage_num} (IMAGE) ===")

                        retry_find_stage = 0
                        while True:
                            self.capture_screen()
                            pos_q151 = self.find_image("img/stage151.png", threshold=0.95)
                            if pos_q151:
                                print(f"[{self.device_id}] !!! STAGE 151 DETECTED !!! Triggering Routine...")
                                if not self.handle_quest_151():
                                    raise AccountFinished()
                                continue

                            on_map = self.find_image("img/waitmainstage.png")

                            if not on_map:
                                print(f"[{self.device_id}] Not on map. Checking for mainstage button...")
                                pos_main = self.find_image("img/mainstage.png", threshold=0.7)
                                if pos_main:
                                    print(f"[{self.device_id}] Clicking mainstage button...")
                                    self.tap(pos_main[0], pos_main[1])
                                    self.wait_for_image("img/waitmainstage.png", timeout=15)
                                    time.sleep(2)
                                    self.capture_screen()
                                else:
                                    print(f"[{self.device_id}] Cannot find map marker or mainstage button. Retrying...")
                                    retry_find_stage += 1
                                    if retry_find_stage >= 10:
                                        print(f"[{self.device_id}] RECOVERY: Stuck 10 times. Clicking backmainstage.png...")
                                        self.wait_and_click("img/backmainstage.png", timeout=5)
                                        retry_find_stage = 0
                                    time.sleep(2)
                                    continue

                            # Default 0.80 for all stages
                            search_threshold = 0.80
                            print(f"[{self.device_id}] Searching for {target_img} in region with threshold {search_threshold}...")
                            pos = self.find_image_in_region(target_img, region, threshold=search_threshold)

                            if pos:
                                # Safety Check: Prevent falsely clicking chest1.png if it overlaps our target
                                is_chest = False
                                if self.screen_bgr is not None and os.path.exists("img/chest1.png"):
                                    template_chest = cv2.imread("img/chest1.png", cv2.IMREAD_COLOR)
                                    if template_chest is not None:
                                        res_chest = cv2.matchTemplate(self.screen_bgr, template_chest, cv2.TM_CCOEFF_NORMED)
                                        loc_chest = np.where(res_chest >= 0.70)
                                        import math
                                        for pt in zip(*loc_chest[::-1]):
                                            cx = int(pt[0] + template_chest.shape[1]/2)
                                            cy = int(pt[1] + template_chest.shape[0]/2)
                                            if math.hypot(pos[0] - cx, pos[1] - cy) < 40:
                                                is_chest = True
                                                break
                                if is_chest:
                                    print(f"[{self.device_id}] WARNING: Target matches chest1.png! Ignoring false positive.")
                                    time.sleep(1)
                                    continue

                            if pos:
                                if stage_num == 31:
                                    print(f"[{self.device_id}] Entering STAGE 31+ LOOP SYSTEM...")
                                    start_from_side = False
                                    
                                    while True:
                                        self.capture_screen()
                                        if self.find_image("img/stage151.png", threshold=0.95):
                                            if not self.handle_quest_151():
                                                raise AccountFinished()
                                            continue

                                        print(f"[{self.device_id}] >>> Phase 1: Navigating (Dictionary Loop Mode)...")
                                        
                                        if start_from_side or self.is_first_31:
                                            print(f"[{self.device_id}]   [Jump Recover] Checking nextstage -> nextnew before side...")
                                            self.process_sequence([
                                                {"img": "img/nextstage.png", "loop": True, "timeout": 10},
                                                {"img": "img/nextnew.png",   "loop": True, "timeout": 10}
                                            ])
                                            start_from_side = False
                                            if self.is_first_31:
                                                p31 = self.find_image_in_region("img/stage/num31.png", region, threshold=0.8)
                                                if p31: 
                                                    self.tap(p31[0], p31[1], label="stage-31-icon (tap 1)")
                                                    time.sleep(1)
                                                    self.tap(p31[0], p31[1], label="stage-31-icon (tap 2)")
                                                    # NEW: After num31, must click next.png
                                                    self.wait_and_click("img/next.png", timeout=10)
                                        else:
                                            inner_nav_success = False
                                            while not inner_nav_success:
                                                print(f"[{self.device_id}]   [Pre-Nav Loop] Checking clearstop -> nextstage -> nextnew...")
                                                self.process_sequence([
                                                    {"img": "img/clearstop.png", "loop": True, "timeout": 10},
                                                    {"img": "img/nextstage.png", "loop": True, "timeout": 10},
                                                    {"img": "img/nextnew.png",   "loop": True, "timeout": 10}
                                                ])
                                                self.capture_screen()
                                                if self.find_image("img/side.png", 0.8) or self.find_image("img/buyhelp.png", 0.8) or self.find_image("img/startnew.png", 0.8):
                                                    print(f"[{self.device_id}]   Map context verified. Proceeding to main Navigation Sequence...")
                                                    inner_nav_success = True
                                                    break
                                                print(f"[{self.device_id}]   Not on Map yet! Looping back to clearstop -> nextstage -> nextnew (Timeout 10s)...")
                                                time.sleep(1)

                                        # Core Battle Transition Seq
                                        nav_seq = [
                                            {"img": "img/side.png", "loop": True, "timeout": None},
                                            {"img": "img/buyhelp.png", "loop": False, "timeout": None},
                                            {"img": "img/startnew.png", "loop": True, "timeout": None}
                                        ]
                                        self.process_sequence(nav_seq)
                                        
                                        battle_res, battle_status = self.handle_battle_31()
                                        if battle_status == "stop": raise AccountFinished()
                                        
                                        if battle_res:
                                            res = self.handle_finish_31()
                                            if res == "stop": raise AccountFinished()
                                            
                                            if self.is_first_31:
                                                print(f"[{self.device_id}]   STAGE 31 SPECIAL AFTER CLEAR (som -> nextstage -> autoadvance1 -> 751,505x8)")
                                                self.wait_and_click("img/som.png", timeout=15)
                                                pos_ns = self.wait_and_click("img/nextstage.png", timeout=10)
                                                if pos_ns: 
                                                    time.sleep(0.5); self.tap(pos_ns[0], pos_ns[1], label="nextstage_repeat")
                                                self.wait_and_click("img/autoadvance.png", timeout=10)
                                                for _ in range(8):
                                                    self.tap(751, 505, label="special_pos_751_505")
                                                    time.sleep(0.3)
                                                self.is_first_31 = False

                                            if res == "jump":
                                                print(f"[{self.device_id}]   JUMP executed! Returning directly to Side...")
                                                start_from_side = True
                                        
                                        time.sleep(1)
                                    # End of Stage 31 loop
                                
                                else:
                                    # --- Standard Stage Handling (Non-31) ---
                                    if stage_num in [22, 23, 28]:
                                        print(f"[{self.device_id}] Stage {stage_num}: Waiting 10s for screen scroll to settle...")
                                        time.sleep(10)
                                        print(f"[{self.device_id}] Stage {stage_num}: Re-detecting final position...")
                                        pos_settled = self.find_image_in_region(target_img, region, threshold=search_threshold)
                                        if pos_settled:
                                            pos = pos_settled
                                        else:
                                            print(f"[{self.device_id}] Stage {stage_num}: Lost target after settle! Retrying loop...")
                                            continue

                                    print(f"[{self.device_id}] Found stage {stage_num} image, tapping...")

                                    if stage_num == 5:
                                        print(f"[{self.device_id}]   Stage 5: Checking for event version (eventstage5.png) in region...")
                                        pos_ev = self.find_image_in_region("img/eventstage5.png", region, threshold=0.8)
                                        if pos_ev:
                                            print(f"[{self.device_id}]     Found event version for Stage 5 on map! Using that.")
                                            pos = pos_ev

                                    self.tap(pos[0], pos[1])
                                    time.sleep(1)

                                    if stage_num == 10:
                                        print(f"[{self.device_id}] Stage 10 special: Checking for drag1...")
                                        time.sleep(1)
                                        pos_drag = self.find_image("img/drag1.png", threshold=0.8)
                                        if pos_drag:
                                            print(f"[{self.device_id}]   Found drag1, performing dynamic drag to 258, 444...")
                                            self.swipe(pos_drag[0], pos_drag[1], 258, 444, duration=1500)
                                            time.sleep(1)

                                    if stage_num == 13:
                                        print(f"[{self.device_id}] Stage 13 special: Delay 2s then check checkstage1...")
                                        time.sleep(2)
                                        if self.wait_and_click("img/checkstage1.png", timeout=10):
                                            print(f"[{self.device_id}]   Found checkstage1, clicking until gone...")
                                            while True:
                                                self.capture_screen()
                                                p = self.find_image("img/checkstage1.png", 0.8)
                                                if p:
                                                    self.tap(p[0], p[1], label="checkstage1")
                                                    time.sleep(0.5)
                                                    continue
                                                break

                                    if stage_num == 25:
                                        print(f"[{self.device_id}] Stage 25 special: Finding checkpoint2.png (timeout 5s)...")
                                        if self.wait_and_click("img/checkpoint2.png", timeout=5):
                                            print(f"[{self.device_id}]   Found! Looping checkpoint2 until gone...")
                                            while self.wait_and_click("img/checkpoint2.png", timeout=3): pass

                                    if stage_num == 30:
                                        print(f"[{self.device_id}] Stage 30 special start sequence...")
                                        stage30_seq = [
                                            {"img": "img/next.png",    "loop": True,  "timeout": None},
                                            {"img": "img/skip.png",    "loop": False, "timeout": None},
                                            {"img": "img/skipok.png",  "loop": False, "timeout": None},
                                            {"img": "img/friends.png", "loop": True,  "timeout": None, "max_clicks": 5},
                                            {"img": "img/start.png",   "loop": True,  "timeout": 30},
                                            {"img": "img/push.png",    "loop": True,  "timeout": None}
                                        ]
                                        self.process_sequence(stage30_seq)
                                        print(f"[{self.device_id}] Stage 30 logic completed, moving to battle.")
                                        break

                                    if self.wait_and_click("img/start.png", timeout=5):
                                        if stage_num == 13:
                                            print(f"[{self.device_id}] Stage 13 special post-start: Checking for auto1.png...")
                                            pos_auto = self.wait_and_click("img/auto1.png", timeout=10, threshold=0.8)
                                            if pos_auto:
                                                print(f"[{self.device_id}]   Found auto1! Clicking 2 times...")
                                                self.tap(pos_auto[0], pos_auto[1], label="auto1-rep1")
                                                time.sleep(0.5)
                                                self.tap(pos_auto[0], pos_auto[1], label="auto1-rep2")
                                        break

                                print(f"[{self.device_id}] Stage {stage_num} image not found. (Retry: {retry_find_stage}/5)")

                        hero_coords = [(278, 521), (384, 514), (483, 508), (582, 515), (683, 517), (146, 483)]

                        if stage_num == 13:
                            print(f"[{self.device_id}]   Stage 13 Battle Logic: checking auto1 (wait 5s)...")
                            pos_auto = self.wait_and_click("img/auto1.png", timeout=5, threshold=0.8)
                            if pos_auto:
                                self.tap(pos_auto[0], pos_auto[1], label="auto1-repeat-1")
                                self.tap(pos_auto[0], pos_auto[1], label="auto1-repeat-2")
                                time.sleep(0.5)

                        win_detected = False
                        stop_spam = False

                        def spam_heroes():
                            print(f"[{self.device_id}]   [Machine-Gun] Tapping started!")
                            burst_coords = hero_coords * 2
                            tap_chain = " & ".join([f"input tap {c[0]} {c[1]}" for c in burst_coords]) + " & wait"
                            while not stop_spam:
                                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", tap_chain], **self.kwargs)
                                time.sleep(0.01)

                        spam_thread = Thread(target=spam_heroes)
                        spam_thread.start()

                        while not win_detected:
                            for i in [2, 1]:
                                self.capture_screen()
                                pos = self.find_image(f"img/useitem{i}.png")
                                if pos: self.tap(pos[0], pos[1])
                            time.sleep(3)
                            self.capture_screen()
                            pos3 = self.find_image("img/useitem3.png")
                            if pos3: self.tap(pos3[0], pos3[1])
                            time.sleep(3)
                            self.capture_screen()
                            pos4 = self.find_image("img/useitem4.png")
                            if pos4: self.tap(pos4[0], pos4[1])

                            self.capture_screen()
                            if self.find_image("img/win.png"):
                                print(f"[{self.device_id}] WIN detected!")
                                stop_spam = True
                                win_detected = True
                                break

                        spam_thread.join()
                        self.handle_clear_routine(stage_num)
                except GameCrashed:
                    print(f"[{self.device_id}] RECOVERY: Game crashed. Retrying account {fname} from login...")
                    time.sleep(2)
                    continue # Re-runs push, login, find_team, then repeat loop for same account
                except AccountFinished:
                    print(f"[{self.device_id}] Account {self.current_account} reached Stage 151 and backed up. Switching...")
                    # Success cleanup: Delete the injected file from backup folder
                    local_orig_path = os.path.join("backup", fname)
                    if os.path.exists(local_orig_path):
                        os.remove(local_orig_path)
                        print(f"[{self.device_id}] Cleaned up backup file: {fname}")
                    self._release_file_lock(fname)
                    break # Exits retry loop, moves to next account in global queue


def main():
    if not find_adb_executable():
        print("ADB not found.")
        return
    connect_known_ports()
    serials = get_connected_devices()
    if not serials:
        print("No devices found.")
        return

    # Startup Cleanup: Remove stale .lock files
    backup_dir = "backup"
    temp_lock_dir = os.path.join(tempfile.gettempdir(), "ranger-locks")
    if os.path.exists(backup_dir):
        for lf in glob.glob(os.path.join(backup_dir, "*.lock")):
            try: os.remove(lf)
            except: pass
    if os.path.exists(temp_lock_dir):
        for lf in glob.glob(os.path.join(temp_lock_dir, "*.lock")):
            try: os.remove(lf)
            except: pass

    threads = []
    for serial in serials:
        bot = BotInstance(serial)
        t = Thread(target=bot.run_step1)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

if __name__ == "__main__":
    main()