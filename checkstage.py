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
import colorama
import ssl
import threading
from datetime import datetime

try:
    import customtkinter as ctk
    from tkinter import messagebox
    GUI_AVAILABLE = True
except ImportError:
    import collections
    ctk = collections.namedtuple('MockCTK', ['CTk', 'CTkToplevel', 'CTkFrame', 'CTkLabel', 'CTkButton', 'CTkEntry', 'CTkTextbox', 'CTkScrollableFrame', 'CTkCheckBox', 'CTkOptionMenu', 'CTkFont', 'set_appearance_mode', 'set_default_color_theme'])(
        *[object]*11, lambda *a, **k: None, lambda *a, **k: None
    )
    messagebox = collections.namedtuple('MockMsgBox', ['showinfo', 'showerror', 'askokcancel'])(*[lambda *a, **k: None]*3)
    GUI_AVAILABLE = False
    print("[WARN] customtkinter or tkinter not found. GUI mode will be disabled. Run 'pip install customtkinter' to enable.")

colorama.init(autoreset=True)

# Fix SSL certificate error for downloading EasyOCR models
ssl._create_default_https_context = ssl._create_unverified_context

# =========================================================
# Statistics and GUI Tracking
# =========================================================
# ----- Simplified UI Stats Class -----
class RestartTimeoutError(Exception): pass

class SimpleUIStats:
    def __init__(self):
        self.total_files = 0
        self.successful_logins = 0
        self.failed_logins = 0
        self.processed_files = 0
        self.connected_devices = 0
        self.lock = threading.RLock()
        self.last_update = time.time()
        self.update_interval = 30
        self.device_statuses = {}
        self.hero_counts = {}
        # Counter สำหรับ hero found/not-found
        self.success_count = 0 # Matches bot success_count
        self.fail_count = 0    # Matches bot fail_count
        self.random_fail_count = 0 # Counter for gacha/swap_shop failures
        # hero found list with counts
        self.hero_found_list = {}  # {hero_combo: count} e.g. {'Yor': 1, 'Yor+Anya': 2}
        self.total_login_time = 0.0
        self.login_time_count = 0
        
    def _get_shared_file(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_stats.json")

    def save_shared(self):
        """Save stats to a shared file for multi-process sync (Atomic write)"""
        try:
            with self.lock:
                data = {
                    "success_count": self.success_count,
                    "fail_count": self.fail_count,
                    "random_fail_count": self.random_fail_count,
                    "hero_found_list": self.hero_found_list,
                    "device_statuses": self.device_statuses,
                    "last_update": time.time(),
                    "total_login_time": getattr(self, "total_login_time", 0),
                    "login_time_count": getattr(self, "login_time_count", 0)
                }
                path = self._get_shared_file()
                tmp_path = path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                
                # Atomic replace with retry for Windows WinError 32
                for _ in range(5):
                    try:
                        os.replace(tmp_path, path)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    # Fallback
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
        except Exception as e:
            print(f"[DEBUG] save_shared error: {e}")

    def load_shared(self):
        """Load stats from the shared file with retries"""
        shared_file = self._get_shared_file()
        if not os.path.exists(shared_file):
            return
            
        for _ in range(5): # Retry up to 5 times
            try:
                with open(shared_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    if not content: continue
                    data = json.loads(content)
                    with self.lock:
                        # Only update if shared data is newer or to merge
                        self.success_count = max(self.success_count, data.get("success_count", 0))
                        self.fail_count = max(self.fail_count, data.get("fail_count", 0))
                        self.random_fail_count = max(self.random_fail_count, data.get("random_fail_count", 0))
                        
                        # Merge hero lists (take max count)
                        shared_heroes = data.get("hero_found_list", {})
                        for h, count in shared_heroes.items():
                            self.hero_found_list[h] = max(self.hero_found_list.get(h, 0), count)
                            
                        # Load login times
                        self.total_login_time = data.get("total_login_time", self.total_login_time)
                        self.login_time_count = data.get("login_time_count", self.login_time_count)
                            
                        # Update device statuses
                        self.device_statuses.update(data.get("device_statuses", {}))
                break
            except Exception as e:
                time.sleep(0.1)

    def record_login_time(self, duration_sec):
        self.load_shared()
        with self.lock:
            self.total_login_time += duration_sec
            self.login_time_count += 1
            self.save_shared()

    def update(self, total=None, processed=None, success=None, fail=None, random_fail=None, devices=None, hero_found=None, hero_not_found=None):
        self.load_shared() # Pull latest from others first to avoid overwriting counts
        with self.lock:
            if total is not None: self.total_files = total
            if processed is not None: self.processed_files = processed
            if success is not None: 
                # For success/fail, we take the max of (local incremented) vs (shared latest)
                # This is safer than just setting it.
                self.success_count = max(self.success_count, success)
            if fail is not None: 
                self.fail_count = max(self.fail_count, fail)
            if random_fail is not None:
                self.random_fail_count = max(self.random_fail_count, random_fail)
            if devices is not None: self.connected_devices = devices
            if hero_found is not None: self.success_count += hero_found
            if hero_not_found is not None: self.fail_count += hero_not_found
            self.save_shared()
    
    def update_device(self, device_serial, status):
        """Update device status and sync with shared file"""
        self.load_shared() # Pull latest from others first
        with self.lock:
            self.device_statuses[device_serial] = status
            self.save_shared() # Save merged state back
    
    def update_hero(self, hero_name, count=1):
        """Update hero found count and sync"""
        self.load_shared() # Pull latest first
        with self.lock:
            if hero_name not in self.hero_found_list:
                self.hero_found_list[hero_name] = 0
            self.hero_found_list[hero_name] += count
            self.save_shared()

    def get_hero_combo_stats(self):
        self.load_shared() # Always refresh before getting
        with self.lock:
            return dict(self.hero_found_list)

ui_stats = SimpleUIStats()
GUI_INSTANCE = None

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
        self.login_done = False


    def log(self, message):
        print(f"[{self.device_id}] {message}")
        if GUI_INSTANCE:
            GUI_INSTANCE.log_to_device(self.device_id, message)

    def load_config(self):

        try:
            with open("configmain.json", "r") as f:
                self.config = json.load(f)
        except:
            self.config = {"getclearquest": 0}

    def push_file(self, local_xml_path):
        remote_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        self.log(f"Injecting file: {local_xml_path} (Robust Mode)...")
        
        # Ensure directories exist
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", "mkdir -p /data/data/com.linecorp.LGRGS/shared_prefs"], **self.kwargs)
        
        # Stop app first
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
        time.sleep(2)
        
        # Kill for certainty
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", "killall -9 com.linecorp.LGRGS 2>/dev/null || true"], **self.kwargs)
        time.sleep(1)

        src = os.path.abspath(local_xml_path)
        tmp = f"/data/local/tmp/temp_pref_{self.device_id.replace(':','_')}.xml"
        final_dir = "/data/data/com.linecorp.LGRGS/shared_prefs"
        final = remote_path
        
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # Delete existing file first
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", f"rm -f {final}"], **self.kwargs)
                
                # 1. Push to temp location
                result = subprocess.run([self.adb_cmd, "-s", self.device_id, "push", src, tmp], capture_output=True, **self.kwargs)
                if result.returncode != 0:
                    self.log(f"Push attempt {attempt} failed.")
                    time.sleep(2)
                    continue
                
                # 2. Copy, set permissions and owner + SYNC
                shell_cmd = (
                    f"su -c '"
                    f"rm -f {final}; " 
                    f"cp {tmp} {final} && "
                    f"chmod 666 {final} && "
                    f"chown $(stat -c %u:%g {final_dir} 2>/dev/null || stat -c %u:%g {final_dir}/.. 2>/dev/null || echo 1000:1000) {final} || true && "
                    f"rm -f {tmp} && "
                    f"sync"
                    f"'"
                )
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", shell_cmd], **self.kwargs)
                
                self.log(f"✓ Injection successful on attempt {attempt}")
                return True
                    
            except Exception as e:
                self.log(f"Attempt {attempt} error: {e}")
                time.sleep(2)
        
        self.log(f"✗ Injection FAILED after {max_retries} attempts!")
        return False

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
            self.log(f"Pulling {src_remote} -> {local_path} (via {temp_remote})...")
            result = subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", temp_remote, local_path], 
                                   capture_output=True, text=True, **self.kwargs)
            
            if result.returncode == 0:
                self.log(f"✓ File pulled successfully to {local_path}")
            else:
                self.log(f"✗ Pull failed: {result.stderr}")
            
            # 4. Clean up temp file
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", 
                           f"su -c 'rm -f {temp_remote}'"], **self.kwargs)
                           
        except Exception as e:
            self.log(f"Pull file error: {e}")
            # Fallback: try direct pull with chmod
            try:
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", 
                               "su", "-c", f"chmod 666 {src_remote}"], **self.kwargs)
                subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", src_remote, local_path], **self.kwargs)
                self.log(f"Fallback pull completed.")
            except Exception as e2:
                self.log(f"Fallback pull also failed: {e2}")

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
                    self.log(f"Opening app via am start (attempt {attempt})...")
                    self.adb_run([
                        self.adb_cmd, "-s", self.device_id, "shell",
                        "am", "start", "-S", "-n",
                        "com.linecorp.LGRGS/com.linecorp.common.activity.LineActivity"
                    ], timeout=10)
                else:
                    self.log(f"Opening app via monkey (attempt {attempt})...")
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
                    self.log(f"✓ App running (PID: {pid}) - attempt {attempt}")
                    return True
                else:
                    self.log(f"✗ App crashed/bounced! (attempt {attempt}) Retrying...")
                    time.sleep(2)
                    
            except Exception as e:
                self.log(f"Error opening app (attempt {attempt}): {e}")
                time.sleep(2)
        
        self.log(f"Failed to open app after 5 attempts!")
        return False

    def main_login(self, current_filename=None):
        self.log(f"Starting Main Login...")
        self._login_fixid_count = 0
        
        # Clear app
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
        time.sleep(2)
        
        if not self.open_app():
            self.log(f"Login ABORTED: Failed to open app.")
            return False

        start_time = time.time()
        while True:
            self.capture_screen()
            
            # Check for stoplogin - indicate login finished
            if self.find_image("img/stoplogin.png", threshold=0.8):
                self.log(f"Found stoplogin.png! Login complete.")
                break
                
            # --- Persistence Checks for alert2 and fixokk ---
            if self.exists_in_cache("img/alert2.png", threshold=0.8):
                if not hasattr(self, '_alert2_start_time') or self._alert2_start_time is None:
                    self._alert2_start_time = time.time()
                    self.log(f"Detected alert2.png (8s wait to restart)...")
                elif time.time() - self._alert2_start_time >= 8:
                    self.log(f"alert2.png persisted 8s! Restarting app/account...")
                    self._alert2_start_time = None
                    return "restart"
            else:
                self._alert2_start_time = None

            if self.exists_in_cache("img/fixokk.png", threshold=0.8):
                if not hasattr(self, '_fixokk_start_time') or self._fixokk_start_time is None:
                    self._fixokk_start_time = time.time()
                    self.log(f"Detected fixokk.png (5s wait to click)...")
                elif time.time() - self._fixokk_start_time >= 5:
                    self.log(f"fixokk.png persisted 5s! Clicking...")
                    self.click("img/fixokk.png")
                    self._fixokk_start_time = None
            else:
                self._fixokk_start_time = None

            # === fixid1.png → failed ทันที ===
            if self.exists_in_cache("img/fixid1.png", threshold=0.95):
                self.log(f"Found fixid1.png! -> login-failed immediately")
                self._login_fixid_count = 0
                self.handle_login_failure()
                return "failed"

            # === fixid.png Check (เช็คทุกรอบ) -> fikcheck -> refresh -> check ===
            if self.exists_in_cache("img/fixid.png", threshold=0.95):
                self._login_fixid_count += 1
                self.log(f"Found fixid.png ({self._login_fixid_count}/3), fikcheck -> refresh -> check...")
                
                if self._login_fixid_count >= 3:
                    self.log(f"fixid limit reached (3 times)! Failing...")
                    self._login_fixid_count = 0
                    self.handle_login_failure()
                    return "failed"
                
                # 1) กด fikcheck
                self.log(f"Step 1: waiting for fikcheck.png (10s timeout)...")
                time.sleep(1.5) # ให้หน้าจอเสถียรหลัง re-route
                for _ in range(10): # Timeout 10s
                    self.capture_screen()
                    if self.exists_in_cache("img/fikcheck.png", threshold=0.8):
                        self.click("img/fikcheck.png", threshold=0.8)
                        self.log(f"Clicked fikcheck.png")
                        time.sleep(2)
                        break
                    time.sleep(1)
                
                # 2) กด refresh
                self.log(f"Step 2: clicking refresh.png (10s timeout)...")
                for _ in range(10): # Timeout 10s
                    self.capture_screen()
                    if self.exists_in_cache("img/refresh.png"):
                        self.click("img/refresh.png")
                        self.log(f"Clicked refresh.png")
                        time.sleep(3)
                        break
                    time.sleep(1)
                
                # 3) รอ check.png แล้วกด
                self.log(f"Step 3: waiting for check.png (60s timeout)...")
                check_wait_start = time.time()
                while time.time() - check_wait_start < 60:
                    self.capture_screen()
                    if self.exists_in_cache("img/check.png"):
                        self.log(f"Found check.png! Clicking...")
                        self.click("img/check.png")
                        time.sleep(2)
                        # หลังกด check -> รอดู fixid ก่อน 2 วิ
                        found_fixid_after_check = False
                        for _ in range(2):
                            self.capture_screen()
                            if self.exists_in_cache("img/fixid.png"):
                                self.log(f"Found fixid.png right after check! Re-routing...")
                                found_fixid_after_check = True
                                break
                            time.sleep(1)
                        
                        if found_fixid_after_check:
                            break

                        if self.exists_in_cache("img/fikcheck.png", threshold=0.8):
                            self.log(f"Found fikcheck.png after check! Clicking...")
                            self.click("img/fikcheck.png", threshold=0.8)
                            time.sleep(1)
                        break
                    time.sleep(1)
                
                continue

            # === เจอ refresh.png (ไม่มี fixid) -> กด refresh -> check ===
            if self.exists_in_cache("img/refresh.png"):
                self.log(f"Found refresh.png (no fixid), clicking refresh -> check...")
                self.click("img/refresh.png")
                time.sleep(3)
                
                check_wait_start = time.time()
                while time.time() - check_wait_start < 60:
                    self.capture_screen()
                    if self.exists_in_cache("img/check.png"):
                        self.log(f"Found check.png! Clicking...")
                        self.click("img/check.png")
                        time.sleep(2)
                        # หลังกด check -> รอดู fixid ก่อน 2 วิ
                        found_fixid_after_check = False
                        for _ in range(2):
                            self.capture_screen()
                            if self.exists_in_cache("img/fixid.png"):
                                self.log(f"Found fixid.png right after check! Re-routing...")
                                found_fixid_after_check = True
                                break
                            time.sleep(1)
                        
                        if found_fixid_after_check:
                            break
                        
                        # หลังกด check -> หา fixok ด้วย
                        self.capture_screen()
                        if self.exists_in_cache("img/fixok.png"):
                            self.log(f"Found fixok.png after check! Clicking...")
                            self.click("img/fixok.png")
                            time.sleep(1)
                        break
                    time.sleep(1)
                
                continue

            # Handle event sequence if found (Precision 0.95)
            if self.exists_in_cache("img/event.png", threshold=0.95):
                pos_ev = self.find_image("img/event.png", threshold=0.95)
                self.log(f"Found event.png, handling (Delay 2s)...")
                self.tap(pos_ev[0], pos_ev[1])
                time.sleep(2) 
                
                back_count: int = 0
                while back_count < 10:
                    self.capture_screen()
                    
                    # ถ้าเจอ cancel ให้กดแล้วหยุดกด back (break)
                    if self.exists_in_cache("img/cancel.png"):
                        self.log(f"Found cancel.png during event, clicking.")
                        self.click("img/cancel.png")
                        time.sleep(2)
                        
                        # ลองกด event.png อีกรอบเผื่อมีอันซ้อน
                        self._raw_capture()
                        if self.click("img/event.png", threshold=0.95):
                            self.log(f"Detected another event.png after cancel, clicked.")
                            time.sleep(2)
                        
                        break # ออกจากลูปกด back
                        
                    if self.find_image("img/stoplogin.png"): 
                        break
                        
                    self.press_back()
                    time.sleep(1.5)
                    back_count += 1
            
            # Common popups
            for img in ["alert2.png", "fixid.png", "fixok.png", "fixid1.png", "fixokk.png"]:
                pos = self.find_image(f"img/{img}")
                if pos:
                    # Specific priority: Try cancel first for OK-type popups
                    if img in ["fixok.png", "alert2.png", "fixokk.png"]:
                        p_cancel = self.find_image("img/cancel.png")
                        if p_cancel:
                            self.tap(p_cancel[0], p_cancel[1], label="cancel-before-ok")
                            time.sleep(1)
                            self.capture_screen()
                            if not self.find_image(f"img/{img}"):
                                continue # Popup gone!
                    
                    self.tap(pos[0], pos[1], label=img)
                    time.sleep(1)

            if time.time() - start_time > 480: # 8 min timeout
                self.log(f"Main login timeout.")
                return False
            time.sleep(1.5)
        return True

    def handle_login_failure(self):
        """Handle login failure by moving account to login-failed/ folder"""
        dst_dir = "login-failed"
        if not os.path.exists(dst_dir): os.makedirs(dst_dir)
        fname = self.current_account or "failed_unknown.xml"
        dst_path = os.path.join(dst_dir, fname)
        
        self.log(f"FAILED LOGIN! Salvaging session to {dst_path}...")
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
                    self.log(f"!!! GAME CRASHED (PID MISSING) !!! Triggering Recovery...")
                    raise GameCrashed()
            except GameCrashed:
                raise # Re-raise to be caught by run_step1
            except Exception:
                pass # ADB or pidof error, ignore for now

        # Popup check to handle common interruptions
        if not getattr(self, "_in_popup_check", False):
            self._in_popup_check = True
            try:
                if not getattr(self, "login_done", False):
                    self.check_floating_popups()
                
                # Independent Scan for Level Verify
                if self.config.get("skip-lv", 0) == 1:
                    self.check_account_level()
            except Exception as e:
                self.log(f"Popup check error: {e}")
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
            self.log(f"Capture error: {e}")
        return False

    def tap(self, x, y, label=None):
        if label:
            self.log(f"Tapping {label} at ({x}, {y})")
        else:
            self.log(f"Tapping screen...")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "tap", str(x), str(y)], **self.kwargs)

    def press_back(self):
        self.log(f"Pressing BACK (ADB shell KEYCODE_BACK)")
        subprocess.run(
            [self.adb_cmd, "-s", self.device_id, "shell", "input", "keyevent", "KEYCODE_BACK"],
            **self.kwargs
        )

    def swipe(self, x1, y1, x2, y2, duration=1000):
        self.log(f"Dragging from ({x1}, {y1}) to ({x2}, {y2}) over {duration}ms...")
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
                self.log(f"...searching for {img_name} ({rem}s remaining of {timeout}s)")
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
            self.log(f"Raw capture error: {e}")
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
                self.log(f"[POPUP] checkline.png detected! Running special sequence...")
                self.click("img/checkline.png")
                time.sleep(2)
                found_any = True
                
                # Wait for check-l1.png (Wait up to 60s)
                start_l1 = time.time()
                while time.time() - start_l1 < 60:
                    self._raw_capture()
                    if self.exists_in_cache("img/check-l1.png"):
                        self.log(f"[POPUP] Found check-l1.png")
                        break
                    time.sleep(1)
                
                # Coordinate taps based on newer system
                self.log(f"[POPUP] Tapping checkbox coordinates (932,133), (930,253), (926,327)...")
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
                self.log(f"[POPUP] Waiting for check-ok1.png...")
                for _ in range(60):
                    self._raw_capture()
                    if self.exists_in_cache("img/check-ok1.png"):
                        self.click("img/check-ok1.png")
                        self.log(f"[POPUP] checkline done.")
                        time.sleep(2)
                        break
                    time.sleep(1)

            # fixok.png / alert2.png / fixokk.png: General Disconnect / Other Device
            if self.exists_in_cache("img/fixok.png") or self.exists_in_cache("img/alert2.png") or self.exists_in_cache("img/fixokk.png"):
                self.log(f"[POPUP] Disconnect/Alert detected! Trying Cancel first...")
                # TRY CANCEL FIRST if available
                if self.exists_in_cache("img/cancel.png"):
                    self.click("img/cancel.png")
                    time.sleep(1)
                
                # Check again, if still there, click specific OK buttons
                self._raw_capture()
                if self.click("img/fixok.png") or self.click("img/alert2.png") or self.click("img/fixokk.png"):
                    time.sleep(2)
                found_any = True

            # fixnetv2.png: Network/Retry Sequence
            if self.exists_in_cache("img/fixnetv2.png"):
                self.log(f"[POPUP] fixnetv2.png detected! Executing retry clicks...")
                if self.exists_in_cache("img/cancel.png"): 
                    self.click("img/cancel.png"); time.sleep(1); self._raw_capture()

                if self.click("img/fixnetv2.png"):
                    time.sleep(3)
                    self.click("img/fixnetv2ok.png")
                    time.sleep(2)
                found_any = True

            # fixplay.png: Google Play Popup
            if self.exists_in_cache("img/fixplay.png"):
                self.log(f"[POPUP] fixplay.png detected! Clicking OK...")
                if self.exists_in_cache("img/cancel.png"): 
                    self.click("img/cancel.png"); time.sleep(1); self._raw_capture()
                self.click("img/ok.png")
                time.sleep(2)
                found_any = True

            # fixnet.png: General Connection Error
            if self.exists_in_cache("img/fixnet.png"):
                self.log(f"[POPUP] fixnet.png detected! Clicking OK Reset...")
                if self.exists_in_cache("img/cancel.png"): 
                    self.click("img/cancel.png"); time.sleep(1); self._raw_capture()
                self.click("img/oknet.png")
                time.sleep(2)
                found_any = True

            # fixnet1.png: Secondary Connection Error
            if self.exists_in_cache("img/fixnet1.png"):
                self.log(f"[POPUP] fixnet1.png detected! Tapping (476, 394)...")
                self.tap(476, 394)
                time.sleep(2)
                found_any = True

            # fixnetv3.png: Force Restart Loop
            if self.exists_in_cache("img/fixnetv3.png"):
                self.log(f"[POPUP] fixnetv3.png detected! Incrementing restart count...")
                self._fixnetv3_count += 1
                if self._fixnetv3_count >= 3:
                    self.log(f"[RESTART] Hit 3x fixnetv3.png! Forcing bot restart...")
                    self._need_restart = True
                    self._fixnetv3_count = 0
                self.click("img/fixnetv3.png")
                time.sleep(2)
                found_any = True

            if not found_any:
                break
            else:
                self._raw_capture() # Update cache for next iteration

    def check_account_level(self):
        """Dedicated level verification with enhanced preprocessing for high accuracy"""
        skip_lv = self.config.get("skip-lv", 0)
        if skip_lv != 1 or getattr(self, "_checklv_done", False):
            return

        if self.exists_in_cache("img/checkpont-lv.png"):
            self.log(f"[CHECK-LV] Target found! Verifying level...")
            self._checklv_done = True
            
            # 1. Capture & Crop Region
            # Region: 25, 17, 81, 74
            region = (25, 17, 81, 74)
            rx, ry, rw, rh = region
            img_crop = self.screen_bgr[ry:ry+rh, rx:rx+rw]
            
            # 2. Advanced Preprocessing for OCR Stability
            # Convert to Grayscale
            gray = cv2.cvtColor(img_crop, cv2.COLOR_BGR2GRAY)
            # Upscale 3x (better for small digits)
            resized = cv2.resize(gray, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
            # Denoise with slight blur
            blurred = cv2.GaussianBlur(resized, (3, 3), 0)
            # Thresholding to get sharp black/white text
            _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            
            import easyocr
            import re
            if self._ocr_reader is None:
                self.log(f"[OCR] Initializing for Level Check...")
                self._ocr_reader = easyocr.Reader(['en'], gpu=False)
                
            # Read text with digital allowlist
            results = self._ocr_reader.readtext(thresh, allowlist='0123456789')
            
            found_lv = None
            max_conf = 0
            for (bbox, text, conf) in results:
                if conf > 0.3: # Higher confidence needed
                    digits = re.findall(r'\d+', text)
                    if digits:
                        val = int(digits[0])
                        # Common sanity check: Level at checkpoint shouldn't be extreme
                        if val > 99: continue 
                        
                        if conf > max_conf:
                            found_lv = val
                            max_conf = conf
            
            if found_lv is not None:
                self.log(f"[CHECK-LV] Detected Level: {found_lv} (Conf: {max_conf:.2f})")
                if found_lv > 4:
                    self.log(f"[CHECK-LV] Level {found_lv} > 4! Moving account to lv5+ and stopping.")
                    # Move file logic
                    if self.current_account:
                        source = os.path.join("backup", self.current_account)
                        dest_dir = "lv5+"
                        if not os.path.exists(dest_dir): os.makedirs(dest_dir)
                        dest = os.path.join(dest_dir, self.current_account)
                        try:
                            import shutil
                            shutil.move(source, dest)
                            self.log(f"Account moved to {dest}")
                        except Exception as e:
                            self.log(f"Error moving account: {e}")
                    
                    # Force Stop and Exit current account
                    subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
                    raise AccountFinished()
                else:
                    self.log(f"[CHECK-LV] Level {found_lv} <= 4. Continuing normally.")
            else:
                self.log(f"[CHECK-LV] Could not read level reliably. Retrying next capture...")
                self._checklv_done = False # Reset flag to try again on next screen update

    def handle_quest_151(self):
        """
        Stage 151 reached! Force stop app, pull account XML,
        save as stage151_OriginalName.xml in backup-id/, then signal finish.
        """
        self.log(f">>> STAGE 151 QUEST ROUTINE STARTED <<<")
        
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
        self.log(f"Backing up account to {dest_path}...")
        # Fix permissions before pulling
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", f"chmod 666 {remote_path}"], **self.kwargs)
        subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", remote_path, dest_path], **self.kwargs)
        
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 100:
            self.log(f"Backup successful: {dest_filename}")
            return False # Return False to signal AccountFinished (Stop current work on this ID)
        else:
            self.log(f"ERROR: Backup failed / File missing!")
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
                    self.log(f"Initializing EasyOCR...")
                    self._ocr_reader = easyocr.Reader(['en'], gpu=False)

                if region:
                    rx, ry, rw, rh = region
                    img_crop = self.screen_bgr[ry:ry+rh, rx:rx+rw]
                    processed_img = cv2.resize(img_crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                results = self._ocr_reader.readtext(processed_img, allowlist='0123456789sS')

                for (bbox, text, conf) in results:
                    numbers_found = re.findall(r'\d+', text)

                    if conf > 0.2:
                        self.log(f"OCR Read: '{text}' (conf: {conf:.2f}) (Digits: {numbers_found})")

                    for target_text in text_list:
                        if target_text.isdigit():
                            if target_text in numbers_found:
                                (tl, tr, br, bl) = bbox
                                center_x = int((tl[0] + br[0]) / 2 / 2) + rx
                                center_y = int((tl[1] + br[1]) / 2 / 2) + ry

                                self.log(f"=> Found '{target_text}', clicking at ({center_x}, {center_y})...")
                                self.tap(center_x, center_y)
                                return True
            except Exception as e:
                self.log(f"OCR Error: {e}")

            time.sleep(1)
        return False

    def advanced_drag_hold(self, points, hold_sec=3):
        p1, p2, p3 = points
        self.log(f"[Double-Drag Hold] Phase 1: {p1}->{p2} (Release)")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                        str(p1[0]), str(p1[1]), str(p2[0]), str(p2[1]), "500"], **self.kwargs)
        time.sleep(1.0)
        self.log(f"[Double-Drag Hold] Phase 2: DragAndDrop {p2} -> {p3} (Focusing Hold)")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "draganddrop",
                        str(p2[0]), str(p2[1]), str(p3[0]), str(p3[1]), str(hold_sec*1000)], **self.kwargs)

    def handle_clear_routine(self, stage_num):
        """Standardized Master Clear Routine (Deep Reward Edition)"""
        self.log(f"Starting clear sequence (Stage {stage_num})...")

        def wc(img, timeout=3): return self.wait_and_click(img, timeout=timeout)
        
        def reward_sweep(label="Reward Sweep", timeout_idle=10):
            self.log(f"{label}: Searching for all reward items...")
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
            self.log(f"{label} finished.")

        # ============================================================
        # STAGE 5 (High Priority - Exact Sequential Logic)
        # ============================================================
        if stage_num == 5:
            self.log(f"Stage 5: Starting 100% Exact Sequential Logic...")
            
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
            self.log(f"Stage 5: Checking for eventstage5.png (10s window)...")
            while True:
                if wc("img/eventstage5.png", 15):
                    time.sleep(1)
                else: 
                    break
            
            # 7. skip (search indefinitely until found) -> skipok 30s -> skip 30s -> skipok 30s
            self.log(f"Stage 5: Waiting indefinitely for skip.png...")
            while not wc("img/skip.png", 15): pass
            wc("img/skipok.png", 15); wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            # 8. quest -> quest1 -> quest2 -> quest3
            wc("img/quest.png", 15); wc("img/quest1.png", 15); wc("img/quest2.png", 15); wc("img/quest3.png", 15)
            
            # 9. mainstage -> wait for waitmainstage.png -> KEYCODE_BACK
            wc("img/mainstage.png", 15)
            if self.wait_for_image("img/waitmainstage.png", timeout=60):
                self.log(f"Found waitmainstage, sending Keycode Back...")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "keyevent", "KEYCODE_BACK"], **self.kwargs)
                time.sleep(1)
            
            # 10. Final eventstage5 check (10s) -> mainstage
            wc("img/eventstage5.png", 15); wc("img/mainstage.png", 15)
            
            self.log(f"Stage 5 Sequence 100% Completed.")
            return True

        # ============================================================
        # STAGE 12 (BOSS) POST-WIN SEQUENCE
        # ============================================================
        if stage_num == 12:
            self.log(f"=== STARTING STAGE 12 (BOSS) POST-WIN SEQUENCE ===")
            reward_sweep("Initial Sweep")
            
            # eventstage11 15s -> skip -> skipok -> gacha1
            wc("img/eventstage11.png", 30)
            wc("img/skip.png", 30)
            wc("img/skipok.png", 15)
            
            # gacha1 -> gacha2 10s -> skip -> skipok
            wc("img/gacha1.png", 30)
            wc("img/gacha2.png", 30)
            wc("img/skip.png", 15)
            wc("img/skipok.png", 15)
            
            # gear1-gear5 -> skip -> skipok
            for g in range(1, 6):
                wc(f"img/gear{g}.png", 15)
            wc("img/skip.png", 15)
            wc("img/skipok.png", 15)
            
            # gearep1 -> skip -> 10s wait -> skipok
            wc("img/gearep1.png", 300)
            wc("img/skip.png", 15)
            time.sleep(10)
            wc("img/skipok.png", 15)
            
            # gearep2 -> skip -> skipok
            wc("img/gearep2.png", 300)
            wc("img/skip.png", 15)
            wc("img/skipok.png", 15)
            
            # fixgear4-1 -> gearep3 -> repeat tap position 5 times
            wc("img/fixgear4-1.png", 30)
            pos_g3 = wc("img/gearep3.png", 50)
            if pos_g3:
                self.log(f"Repeating tap on gearep3 x9...")
                for _ in range(9):
                    self.tap(pos_g3[0], pos_g3[1], label="gearep3-repeat")
                    time.sleep(0.5)
            
            # gearep4 -> skip -> skip 10s -> backgearep1 -> mainstage
            wc("img/gearep4.png", 300)
            wc("img/skip.png", 15)
            time.sleep(10)
            
            self.log(f"Stage 12: Tapping backgearep1.png until gone...")
            while wc("img/backgearep1.png", 5):
                time.sleep(0.5)
            wc("img/mainstage.png", 80)
            
            self.log(f"=== STAGE 12 SEQUENCE FINISHED ===")
            return True

        # ============================================================
        # STAGE 13 POST-WIN SEQUENCE
        # ============================================================
        if stage_num == 13:
            self.log(f"=== STAGE 13 POST-WIN SEQUENCE ===")
            reward_sweep("Initial Sweep")
            wc("img/mainstage.png", 50)
            self.log(f"Stage 13 Sequence Completed.")
            return True

        # ============================================================
        # STAGE 14 POST-WIN SEQUENCE (Standard)
        # ============================================================
        if stage_num == 14:
            self.log(f"=== STAGE 14 POST-WIN SEQUENCE ===")
            reward_sweep("Initial Sweep")
            wc("img/skip.png", 30); wc("img/skipok.png", 20); wc("img/mainstage.png", 20)
            reward_sweep("Default Final Cleanup", timeout_idle=5)
            self.log(f"Stage 14 Sequence Completed.")
            return True

        # ============================================================
        # STAGE 15 (Exact Sequential Logic + Initial Sweep)
        # ============================================================
        if stage_num == 15:
            self.log(f"Stage 15: First Full Clearing...")
            reward_sweep("Initial Sweep")
            self.log(f"Stage 15: Starting Exact Sequential Logic...")
            wc("img/skip.png", 50); wc("img/skip.png", 50); wc("img/skipok.png", 50)
            # Loop egear1 -> 5 indefinitely until found, then tap until gone
            last_eg5_pos = None
            for img_name in ["egear1.png", "egear2.png", "egear3.png", "egear4.png", "egear5.png"]:
                self.log(f"Waiting indefinitely for {img_name}...")
                while True:
                    self.capture_screen()
                    p = self.find_image(f"img/{img_name}", 0.8)
                    if p: break
                    time.sleep(0.5)

                self.log(f"Found {img_name}, looping until gone...")
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
                self.log(f"Delaying 2s before eg5 extra taps...")
                time.sleep(2)
                for _ in range(8): 
                    self.tap(last_eg5_pos[0], last_eg5_pos[1], label="eg5-repeat")
                    time.sleep(2.0)
            
            wc("img/skip.png", 15); wc("img/skipok.png", 15)

            # Loop backegear until gone
            self.log(f"Looping backegear.png until gone...")
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
            self.log(f"Stage 27 special: Waiting 15s before starting clear sweep...")
            time.sleep(15)
            
        reward_sweep("Initial Sweep")

        if stage_num == 10:
            self.log(f"Stage 10: Starting update and finishing flow...")
            wc("img/skip.png", 15); wc("img/skipok.png", 15)
            
            while True:
                self.capture_screen()
                u1 = self.find_image("img/update1.png", 0.8); u2 = self.find_image("img/update2.png", 0.8)
                if u1: self.tap(u1[0], u1[1], label="update1"); continue
                if u2: self.tap(u2[0], u2[1], label="update2"); continue
                pos_drag = self.find_image("img/drag1.png", threshold=0.85)
                if pos_drag:
                    self.log(f"Found drag1 at {pos_drag}, Sweeping from 99, 447 to 395, 259 (800ms)...")
                    subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", "99", "447", "395", "259", "800"], **self.kwargs)
                    time.sleep(1); break
                time.sleep(1)
            
            # Click update3 and repeat slowly (6 reps with 2s delay)
            p_u3 = self.wait_and_click("img/update3.png", timeout=20)
            if p_u3:
                self.log(f"Clicking update3 repeats (Slower: 2s delay)...")
                for _ in range(5): 
                    time.sleep(2.0)
                    self.tap(p_u3[0], p_u3[1], label="update3-repeat")
            
            # Finishing Flow (Ensuring it gets back to map for Stage 11)
            self.log(f"Stage 10: Executing finishing sequence...")
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

            self.log(f"SeqWait for {img_name}... (Timeout: {to or 'Infinite'})")
            start_wait = time.time()
            found = False
            while True:
                if to and (time.time() - start_wait > float(to)):
                    break
                self.capture_screen()

                if self.find_image(img_path, 0.8):
                    found = True
                    break
                time.sleep(0.5)
            
            if not found:
                self.log(f"{img_name} not found, skipping.")
                if is_critical:
                    return False
                continue
            
            self.log(f"Found {img_name}! Executing '{'loop-click' if is_loop else 'single-click'}'...")
            clicks = 0
            while True:
                self.capture_screen()
                pos = self.find_image(img_path, 0.8)
                if not pos:
                    break # Success! Image is gone
                
                self.tap(pos[0], pos[1], label=img_name)
                clicks += 1
                
                if max_clicks and clicks >= max_clicks:
                    self.log(f"Max clicks ({max_clicks}) reached for {img_name} ({clicks}/{max_clicks}).")
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
                self.log(f">>> STAGE 151 DETECTED: AUTO-BACKUP & SWITCH (getclearquest=0) <<<")
                
                # 1. Clear Game
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
                time.sleep(2)

                # 2. Backup File
                backup_dir = "backup-id"
                if not os.path.exists(backup_dir): os.makedirs(backup_dir)
                orig_name = self.current_account or "unknown.xml"
                dest_path = os.path.join(backup_dir, f"stage151_{orig_name}")
                
                self.log(f"Backup Mode (getclearquest: 0) -> Pulling Account to {dest_path}...")
                self.pull_file(dest_path)
                self.log(f"Account saved. Switching to next ID.")
                return False # Signal to break per-account loop
            
            else:
                self.log(f">>> STAGE 151 QUEST ROUTINE STARTED (getclearquest=1) <<<")
                
                # Initial setup (Using timeout=99999 from user snippet)
                self.wait_and_click("img/quest-stage1.png", timeout=99999)
                self.wait_and_click("img/getquse1.png", timeout=99999)
                self.wait_and_click("img/okquest.png", timeout=99999)

                # Play 1 Quest
                self.log(f"Processing Play 1 Quest...")
                self.wait_and_click("img/goquse1.png", timeout=99999)
                pos_pq1 = self.wait_and_click("img/playques1.png", timeout=99999)
                if pos_pq1:
                    self.tap(pos_pq1[0], pos_pq1[1], label="playques1_rep")
                
                self.log(f"Delaying 3s then tapping coord 125,29 & 393,449...")
                time.sleep(3)
                self.tap(125, 29, label="coord_125_29")
                time.sleep(1)
                self.tap(393, 449, label="coord_393_449")
                time.sleep(1)

                for q_img in ["img/playquest2.png", "img/playquest3.png", "img/playquest4.png"]:
                    self.wait_and_click(q_img, timeout=99999)

                self.log(f"Executing quest swipe...")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", "99", "447", "395", "259", "800"], **self.kwargs)
                time.sleep(1)

                self.wait_and_click("img/playquest5.png", timeout=99999)
                self.wait_and_click("img/playquest6.png", timeout=99999)

                # Play 2 Quest
                self.log(f"Processing Play 2 Quest sequence...")
                self.log(f"Tapping coordinate 282, 196 twice...")
                for _ in range(2):
                    self.tap(282, 196, label="coord_282_196")
                    time.sleep(0.5)

                self.wait_and_click("img/back-mainstage.png", timeout=99999)
                self.wait_and_click("img/back-mainstage1.png", timeout=99999)
                self.wait_and_click("img/quest-stage1.png", timeout=99999)
                self.wait_and_click("img/getquse1.png", timeout=99999)
                self.wait_and_click("img/okquest.png", timeout=99999)
                self.wait_and_click("img/goquse1.png", timeout=99999)
                
                self.log(f">>> STAGE 151 QUEST ROUTINE FINISHED <<<")
                return True
        finally:
            self.in_quest_routine = False

    def handle_battle_31(self):
        self.log(f">>> Phase 2: Battle 31+ System...")
        
        # 1. Burst start
        hero_coords = [(278, 521), (384, 514), (483, 508), (582, 515), (683, 517), (146, 483)]
        for _ in range(6):
            tap_chain = " & ".join([f"input tap {c[0]} {c[1]}" for c in hero_coords]) + " & wait"
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", tap_chain], **self.kwargs)
            time.sleep(0.1)

        # 2. Priority Position 7
        self.log(f"Priority Position 7 (50, 43) x5 FIRST")
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
                self.log(f"Interrupt: Stage 151 detected in battle!")
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
                self.log(f"WIN detected!")
                stop_spam = True
                win_detected = True
                break
            time.sleep(0.5)
            
        spam_thread.join()
        return True, "win"

    def handle_finish_31(self):
        self.log(f">>> Phase 3: Finish & Rewards (Priority Loop)...")
        
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
                self.log(f"!!! FLOATING NEXTSTAGE DETECTED !!! Tapping reward pos 218, 46 x6...")
                for _ in range(6):
                    time.sleep(2.0)
                    self.tap(218, 46)
                self.log(f"Bypassing the rest of the clear sequence.")
                return "jump"

            # Check for NextNew
            pos_nn = self.find_image("img/nextnew.png", threshold=0.8)
            if pos_nn:
                self.log(f"!!! FLOATING NEXTNEW DETECTED !!! Tapping 162, 32 -> Jump.")
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
                    self.log(f"Detected map elements. Finishing Phase 3.")
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

        self.log(f"==================================================")
        self.log(f"FIND TEAM - START")
        self.log(f"==================================================")

        self.log(f"STEP 1: Clicking [team]...")
        while not self.wait_and_click("img/team.png", timeout=60, threshold=0.8):
            time.sleep(1)

        time.sleep(1)

        self.log(f"STEP 2: Waiting for [waitteam]...")
        while True:
            if self.wait_for_image("img/waitteam.png", timeout=60, threshold=0.8):
                self.log(f"waitteam found! Proceeding...")
                break
            self.log(f"waitteam not found yet, retrying...")
            time.sleep(1)
        time.sleep(1)

        self.log(f"STEP 3: Clearing all hero slots...")
        for i, slot in enumerate(HERO_SLOTSCLear):
            self.log(f"Clearing hero{i+1}: ({slot[0]}, {slot[1]}) -> ({CLEAR_DROP[0]}, {CLEAR_DROP[1]})")
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                            str(slot[0]), str(slot[1]), str(CLEAR_DROP[0]), str(CLEAR_DROP[1]), "500"], **self.kwargs)
            time.sleep(0.3)
        time.sleep(0.5)

        self.log(f"STEP 4: Applying filters...")
        filters = ["img/filter1.png", "img/filter2.png", "img/filter3.png", "img/filter4.png", "img/filter5.png"]
        for f_img in filters:
            f_name = os.path.basename(f_img)
            self.log(f"Clicking {f_name}...")
            if self.wait_and_click(f_img, timeout=15, threshold=0.95):
                time.sleep(0.3)
            else:
                self.log(f"WARNING: {f_name} not found, continuing anyway...")
        time.sleep(0.5)

        self.log(f"STEP 5: Scanning and dragging heroes...")
        filled_count = 0
        scroll_count = 0
        max_scroll = 20
        dragged_values = []

        while filled_count < 5 and scroll_count <= max_scroll:
            self.log(f"Scanning region {REGION_SCAN}...")
            candidates = scanner.find_numbers_in_region(REGION_SCAN)
            under_target = [c for c in candidates if c["val"] < TARGET_VALUE and c["val"] >= 100 and c["conf"] >= 0.5]

            if under_target:
                hero_info = [f"{c['val']}(conf:{c['conf']:.2f})" for c in under_target]
                self.log(f"Found candidate(s): {hero_info}")

                for hero in under_target:
                    if filled_count >= 5: break

                    if hero["val"] in dragged_values:
                        self.log(f"Skipping hero {hero['val']} - Identical value already in team.")
                        continue

                    target_slot = HERO_SLOTS[filled_count]
                    sx, sy = hero["pos"]

                    self.log(f"Dragging hero {hero['val']} from ({sx}, {sy}) -> hero{filled_count+1} {target_slot}")

                    if target_slot in [(622, 248), (768, 251)]:
                        self.advanced_drag_hold([(sx, sy), REST_POINT, target_slot], hold_sec=3)
                    else:
                        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                                        str(sx), str(sy), str(target_slot[0]), str(target_slot[1]), "500"], **self.kwargs)

                    time.sleep(5)
                    dragged_values.append(hero["val"])
                    filled_count += 1
                    self.log(f"Slots filled: {filled_count}/5 (Team Values: {dragged_values})")

            if filled_count < 5:
                scroll_count += 1
                self.log(f"Scrolling to next candidate (attempt {scroll_count}/{max_scroll})...")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe",
                                "254", "442", "193", "447", "1000"], **self.kwargs)
                time.sleep(3)
            else:
                self.log(f"Find team complete, stopping scan.")
                break

        if filled_count >= 5:
            self.log(f"All 5 hero slots filled!")
        else:
            self.log(f"Could only fill {filled_count}/5 slots after {scroll_count} scrolls.")

        self.log(f"STEP 6: Saving team...")
        time.sleep(1)
        self.log(f"Clicking [backhero]...")
        self.wait_and_click("img/backhero.png", timeout=15, threshold=0.8)
        time.sleep(1)
        self.log(f"Clicking [saveteam]...")
        self.wait_and_click("img/saveteam.png", timeout=15, threshold=0.8)
        time.sleep(1)
        self.log(f"==================================================")
        self.log(f"FIND TEAM - COMPLETED!")
        self.log(f"==================================================")

    def run_step1(self):
        while True:
            fname = self._get_next_available_file()
            if not fname:
                self.log(f"No available accounts in queue. Waiting 10s...")
                time.sleep(10)
                continue
                
            self.current_account = fname
            self._checklv_done = False
            self.login_done = False # Reset for new account
            self.log(f">>> PROCESSING NEW ACCOUNT: {fname} <<<")
            
            while True: # RETRY LOOP (Current Account)
                try:
                    if self._need_restart:
                        self.log(f"RECOVERY: Need restart flag set. Restarting account flow...")
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
                        self.log(f"!! Login failed for {fname}. Moving to next.")
                        self._release_file_lock(fname)
                        break
                    
                    self.login_done = True # ✅ Login finished, stop popup checks
                    self.log(f"Login complete for {fname}. Routine starting...")
            
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

                        self.log(f"=== TARGET STAGE: {stage_num} (IMAGE) ===")

                        retry_find_stage = 0
                        while True:
                            self.capture_screen()
                            pos_q151 = self.find_image("img/stage151.png", threshold=0.95)
                            if pos_q151:
                                self.log(f"!!! STAGE 151 DETECTED !!! Triggering Routine...")
                                if not self.handle_quest_151():
                                    raise AccountFinished()
                                continue

                            on_map = self.find_image("img/waitmainstage.png")

                            if not on_map:
                                self.log(f"Not on map. Checking for mainstage button...")
                                pos_main = self.find_image("img/mainstage.png", threshold=0.7)
                                if pos_main:
                                    self.log(f"Clicking mainstage button...")
                                    self.tap(pos_main[0], pos_main[1])
                                    self.wait_for_image("img/waitmainstage.png", timeout=15)
                                    time.sleep(2)
                                    self.capture_screen()
                                else:
                                    self.log(f"Cannot find map marker or mainstage button. Retrying...")
                                    retry_find_stage += 1
                                    if retry_find_stage >= 10:
                                        self.log(f"RECOVERY: Stuck 10 times. Clicking backmainstage.png...")
                                        self.wait_and_click("img/backmainstage.png", timeout=5)
                                        retry_find_stage = 0
                                    time.sleep(2)
                                    continue

                            # Default 0.80 for all stages
                            search_threshold = 0.80
                            self.log(f"Searching for {target_img} in region with threshold {search_threshold}...")
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
                                    self.log(f"WARNING: Target matches chest1.png! Ignoring false positive.")
                                    time.sleep(1)
                                    continue

                            if pos:
                                if stage_num == 31:
                                    self.log(f"Entering STAGE 31+ LOOP SYSTEM...")
                                    start_from_side = False
                                    
                                    while True:
                                        self.capture_screen()
                                        if self.find_image("img/stage151.png", threshold=0.95):
                                            if not self.handle_quest_151():
                                                raise AccountFinished()
                                            continue

                                        self.log(f">>> Phase 1: Navigating (Dictionary Loop Mode)...")
                                        
                                        if start_from_side or self.is_first_31:
                                            self.log(f"[Jump Recover] Checking nextstage -> nextnew before side...")
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
                                                self.log(f"[Pre-Nav Loop] Checking clearstop -> nextstage -> nextnew...")
                                                self.process_sequence([
                                                    {"img": "img/clearstop.png", "loop": True, "timeout": 10},
                                                    {"img": "img/nextstage.png", "loop": True, "timeout": 10},
                                                    {"img": "img/nextnew.png",   "loop": True, "timeout": 10}
                                                ])
                                                self.capture_screen()
                                                if self.find_image("img/side.png", 0.8) or self.find_image("img/buyhelp.png", 0.8) or self.find_image("img/startnew.png", 0.8):
                                                    self.log(f"Map context verified. Proceeding to main Navigation Sequence...")
                                                    inner_nav_success = True
                                                    break
                                                self.log(f"Not on Map yet! Looping back to clearstop -> nextstage -> nextnew (Timeout 10s)...")
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
                                                self.log(f"STAGE 31 SPECIAL AFTER CLEAR (som -> nextstage -> autoadvance1 -> 751,505x8)")
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
                                                self.log(f"JUMP executed! Returning directly to Side...")
                                                start_from_side = True
                                        
                                        time.sleep(1)
                                    # End of Stage 31 loop
                                
                                else:
                                    # --- Standard Stage Handling (Non-31) ---
                                    if stage_num in [22, 23, 28]:
                                        self.log(f"Stage {stage_num}: Waiting 10s for screen scroll to settle...")
                                        time.sleep(10)
                                        self.log(f"Stage {stage_num}: Re-detecting final position...")
                                        pos_settled = self.find_image_in_region(target_img, region, threshold=search_threshold)
                                        if pos_settled:
                                            pos = pos_settled
                                        else:
                                            self.log(f"Stage {stage_num}: Lost target after settle! Retrying loop...")
                                            continue

                                    self.log(f"Found stage {stage_num} image, tapping...")

                                    if stage_num == 5:
                                        self.log(f"Stage 5: Checking for event version (eventstage5.png) in region...")
                                        pos_ev = self.find_image_in_region("img/eventstage5.png", region, threshold=0.8)
                                        if pos_ev:
                                            self.log(f"Found event version for Stage 5 on map! Using that.")
                                            pos = pos_ev

                                    self.tap(pos[0], pos[1])
                                    time.sleep(1)

                                    if stage_num == 10:
                                        self.log(f"Stage 10 special: Checking for drag1...")
                                        time.sleep(1)
                                        pos_drag = self.find_image("img/drag1.png", threshold=0.8)
                                        if pos_drag:
                                            self.log(f"Found drag1, performing dynamic drag to 258, 444...")
                                            self.swipe(pos_drag[0], pos_drag[1], 258, 444, duration=1500)
                                            time.sleep(1)

                                    if stage_num == 13:
                                        self.log(f"=== STAGE 13 SPECIAL START SEQUENCE ===")
                                        time.sleep(2)
                                        self.wait_and_click("img/checkstage1.png", timeout=10)
                                        self.wait_and_click("img/start.png", timeout=5)
                                        
                                        # Wait for battle load then check auto1
                                        self.log(f"Simulating Stage 13 Battle (check auto1)...")
                                        time.sleep(5)
                                        pos_auto = self.wait_and_click("img/auto1.png", timeout=5, threshold=0.8)
                                        if pos_auto:
                                            self.tap(pos_auto[0], pos_auto[1], label="auto1-repeat-1")
                                            self.tap(pos_auto[0], pos_auto[1], label="auto1-repeat-2")
                                        break

                                    if stage_num == 25:
                                        self.log(f"Stage 25 special: Finding checkpoint2.png (timeout 5s)...")
                                        if self.wait_and_click("img/checkpoint2.png", timeout=5):
                                            self.log(f"Found! Looping checkpoint2 until gone...")
                                            while self.wait_and_click("img/checkpoint2.png", timeout=3): pass

                                    if stage_num == 30:
                                        self.log(f"Stage 30 special start sequence...")
                                        stage30_seq = [
                                            {"img": "img/next.png",    "loop": True,  "timeout": None},
                                            {"img": "img/skip.png",    "loop": False, "timeout": None},
                                            {"img": "img/skipok.png",  "loop": False, "timeout": None},
                                            {"img": "img/friends.png", "loop": True,  "timeout": None, "max_clicks": 5},
                                            {"img": "img/start.png",   "loop": True,  "timeout": 30},
                                            {"img": "img/push.png",    "loop": True,  "timeout": None}
                                        ]
                                        self.process_sequence(stage30_seq)
                                        self.log(f"Stage 30 logic completed, moving to battle.")
                                        break

                                    if self.wait_and_click("img/start.png", timeout=5):
                                        break

                                self.log(f"Stage {stage_num} image not found. (Retry: {retry_find_stage}/5)")

                        hero_coords = [(278, 521), (384, 514), (483, 508), (582, 515), (683, 517), (146, 483)]

                        if stage_num == 13:
                            self.log(f"Stage 13 Battle Logic: checking auto1 (wait 5s)...")
                            pos_auto = self.wait_and_click("img/auto1.png", timeout=5, threshold=0.8)
                            if pos_auto:
                                self.tap(pos_auto[0], pos_auto[1], label="auto1-repeat-1")
                                self.tap(pos_auto[0], pos_auto[1], label="auto1-repeat-2")
                                time.sleep(0.5)

                        win_detected = False
                        stop_spam = False

                        def spam_heroes():
                            self.log(f"[Machine-Gun] Tapping started!")
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
                                self.log(f"WIN detected!")
                                stop_spam = True
                                win_detected = True
                                break

                        spam_thread.join()
                        self.handle_clear_routine(stage_num)
                except GameCrashed:
                    self.log(f"RECOVERY: Game crashed. Retrying account {fname} from login...")
                    time.sleep(2)
                    continue # Re-runs push, login, find_team, then repeat loop for same account
                except AccountFinished:
                    self.log(f"Account {self.current_account} reached Stage 151 and backed up. Switching...")
                    # Success cleanup: Delete the injected file from backup folder
                    local_orig_path = os.path.join("backup", fname)
                    if os.path.exists(local_orig_path):
                        os.remove(local_orig_path)
                        self.log(f"Cleaned up backup file: {fname}")
                    self._release_file_lock(fname)
                    break # Exits retry loop, moves to next account in global queue





ui_stats = SimpleUIStats()
GUI_INSTANCE = None


# --- GUI Components (Defined globally for static analysis) ---
class MainConfigWindow(ctk.CTkToplevel):
    """Window to edit configmain.json settings (System Only)"""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("⚙️ ตั้งค่าระบบ")
        self.geometry("400x350")
        self.parent = parent
        
        self.transient(parent)
        self.grab_set()
        self.focus_force()
        
        self.cfg: dict = self.load_config() or {}
        
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        ctk.CTkLabel(main_frame, text="⚙️ SYSTEM SETTINGS", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(10, 15))
        
        # Thread Delay
        delay_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        delay_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(delay_frame, text="หน่วงเวลาต่อเครื่อง (วินาที):", anchor="w").pack(side="left")
        self.delay_entry = ctk.CTkEntry(delay_frame, width=80)
        self.delay_entry.insert(0, str(self.cfg.get("thread_delay", 5)))
        self.delay_entry.pack(side="right")

        # Get Clear Quest
        quest_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        quest_frame.pack(fill="x", padx=10, pady=5)
        self.var_quest = ctk.BooleanVar(value=bool(self.cfg.get("getclearquest", 0)))
        ctk.CTkSwitch(quest_frame, text="เก็บเควสด่าน 151 (Get Quest)", variable=self.var_quest).pack(side="left")

        # Skip LV
        skip_lv_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        skip_lv_frame.pack(fill="x", padx=10, pady=5)
        self.var_skip_lv = ctk.BooleanVar(value=bool(self.cfg.get("skip-lv", 0)))
        ctk.CTkSwitch(skip_lv_frame, text="เช็ค LV (ข้ามถ้า LV.5+)", variable=self.var_skip_lv).pack(side="left")

        
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(btn_frame, text="💾 บันทึก", command=self.save, fg_color="#2cc985", hover_color="#229f69", width=120).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="❌ ยกเลิก", command=self.destroy, fg_color="#555555", hover_color="#444444", width=100).pack(side="right", padx=5)
    
        ctk.CTkButton(
            bottom_bar, text="📄 Open Logs", width=85, height=22,
            font=ctk.CTkFont(size=10), fg_color="#1565c0",
            command=lambda: subprocess.Popen(f'explorer "{self.log_dir}"')
        ).pack(side="left", padx=3, pady=4)


    def load_config(self):
        try:
            if os.path.exists('configmain.json'):
                with open('configmain.json', 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
        return {}
        
    def save(self):
        try:
            self.cfg["getclearquest"] = 1 if self.var_quest.get() else 0
            self.cfg["skip-lv"] = 1 if self.var_skip_lv.get() else 0
            
            try:
                self.cfg["thread_delay"] = int(self.delay_entry.get())
            except:
                self.cfg["thread_delay"] = 5
            
            with open('configmain.json', 'w', encoding='utf-8') as f:
                json.dump(self.cfg, f, indent=4, ensure_ascii=False)
            
            messagebox.showinfo("สำเร็จ", "บันทึกตั้งค่าระบบเรียบร้อยแล้ว!")
            self.parent.log("INFO", "✅ System Config Updated")
            self.destroy()
        except Exception as e:
            messagebox.showerror("Error", f"บันทึกไม่สำเร็จ: {e}")






class DeviceMonitorWidget(ctk.CTkFrame):
    def __init__(self, parent_gui, device_id, index):
        super().__init__(parent_gui.dev_scroll, fg_color="#383838", corner_radius=6, height=36)
        self.device_id = device_id
        self.parent_gui = parent_gui
        self.pack_propagate(False)
        
        chk = ctk.CTkCheckBox(self, text="", width=20, height=20, checkbox_width=16, checkbox_height=16)
        chk.pack(side="left", padx=(6, 2))
        chk.select()
        
        ctk.CTkLabel(self, text=f"#{index}", font=ctk.CTkFont(size=11, weight="bold"), text_color="#ffffff", width=25).pack(side="left", padx=(0, 4))
        
        name_frame = ctk.CTkFrame(self, fg_color="transparent")
        name_frame.pack(side="left", fill="y", padx=2)
        
        self.lbl_id = ctk.CTkLabel(name_frame, text=device_id, font=ctk.CTkFont(family="Consolas", size=10), text_color="#ccc", anchor="w")
        self.lbl_id.pack(padx=0, pady=(2, 0))
        
        self.lbl_step = ctk.CTkLabel(name_frame, text="Ready", font=ctk.CTkFont(size=9), text_color="#888", anchor="w")
        self.lbl_step.pack(padx=0, pady=(0, 2))
        
        # Action Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(side="right", padx=4)
        
        ctk.CTkButton(btn_frame, text="Log", width=40, height=22, font=ctk.CTkFont(size=10, weight="bold"), 
                     fg_color="#454545", hover_color="#555555", 
                     command=lambda: self.parent_gui.open_device_log(self.device_id)).pack(side="right", padx=2)
        
        self.lbl_status = ctk.CTkLabel(btn_frame, text="READY", font=ctk.CTkFont(size=10, weight="bold"), text_color="#888", width=60)
        self.lbl_status.pack(side="right", padx=6)

    def update_state(self, status=None, step=None):
        if status:
            color_map = {'working': "#4caf50", 'waiting': "#ff9800", 'error': "#e53935", 'idle': "#888", 'success': "#2ecc71"}
            self.lbl_status.configure(text=status.upper(), text_color=color_map.get(status.lower(), "#888"))
        if step:
            self.lbl_step.configure(text=step)


class ModernBotGUI(ctk.CTk):
    # Class-level declarations for static analysis inference
    device_monitors = {}
    device_logs = {}

    hero_stats_labels = {}
    hero_rows = {}
    hero_filter_text = ""
    is_started = False

    def __init__(self, devices, args):
        super().__init__()
        global GUI_INSTANCE
        GUI_INSTANCE = self
        
        self.title("loginสะสม")
        self.geometry("720x550")
        self.devices = devices
        self.args = args
        self.bot_threads = []
        self.device_monitors = {}
        self.device_logs = {} # Logs grouped by device_id

        self.hero_stats_labels = {}

        self.hero_rows = {}
        self.hero_filter_text = ""
        self.is_started = False
        self.log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self._system_log_path = os.path.join(self.log_dir, f"system_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

        self.setup_ui()
        
        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Use after to start the stats loop without blocking the constructor
        self.after(100, self.update_realtime_stats)
        
        # Ensure window is visible
        self.deiconify()
        self.focus_force()
        print("[GUI] Launched Successfully. Waiting for manual start.")
        
        if getattr(self.args, 'no_start', False):
            print("[GUI] Monitor mode active (No internal threads).")
            self.lbl_auto_start.configure(text="[ DASHBOARD MODE ]", text_color="#ffae42")
        else:
            self.lbl_auto_start.configure(text="[ WAITING FOR START ]", text_color="#aaaaaa")

    def setup_ui(self):
        # 1. TOP TOOLBAR
        toolbar = ctk.CTkFrame(self, height=45, fg_color="#333333", corner_radius=0)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        
        self.lbl_status = ctk.CTkLabel(toolbar, text=f"   ● ONLINE ({len(self.devices)})", font=ctk.CTkFont(size=12, weight="bold"), text_color="#4caf50")
        self.lbl_status.pack(side="left", padx=5)

        self.btn_start = ctk.CTkButton(toolbar, text="▶ START ALL", font=ctk.CTkFont(size=12, weight="bold"), width=100, height=28, fg_color="#e53935", hover_color="#c62828", command=self.start_bot)
        self.btn_start.pack(side="left", padx=10)
        
        self.lbl_auto_start = ctk.CTkLabel(toolbar, text="[ READY ]", font=ctk.CTkFont(size=10, weight="bold"), text_color="#aaaaaa")
        self.lbl_auto_start.pack(side="left", padx=5)
        # Stats on Toolbar (right)
        counter_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        counter_frame.pack(side="right", padx=10)
        
        self.lbl_file_count = ctk.CTkLabel(counter_frame, text="📁 0", font=ctk.CTkFont(size=12, weight="bold"), text_color="#aaaaaa")
        self.lbl_file_count.pack(side="left", padx=8)

        self.lbl_succ_count = ctk.CTkLabel(counter_frame, text="✅ 0", font=ctk.CTkFont(size=12, weight="bold"), text_color="#4caf50")
        self.lbl_succ_count.pack(side="left", padx=8)
        
        self.lbl_fail_count = ctk.CTkLabel(counter_frame, text="❌ 0", font=ctk.CTkFont(size=12, weight="bold"), text_color="#ff5555")
        self.lbl_fail_count.pack(side="left", padx=8)

        self.lbl_random_fail = ctk.CTkLabel(counter_frame, text="🎲 0", font=ctk.CTkFont(size=12, weight="bold"), text_color="#ffa500")
        self.lbl_random_fail.pack(side="left", padx=8)
        
        self.lbl_avg_time = ctk.CTkLabel(toolbar, text="Avg: -", font=ctk.CTkFont(size=11), text_color="#2196f3")
        self.lbl_avg_time.pack(side="right", padx=10)
        
        # 2. MAIN CONTENT
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=6, pady=4)
        main_frame.grid_columnconfigure(0, weight=4)
        main_frame.grid_columnconfigure(1, weight=3)
        main_frame.grid_rowconfigure(0, weight=1)
        
        # Left: Devices
        left_frame = ctk.CTkFrame(main_frame, fg_color="#2b2b2b", corner_radius=8)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        
        dev_header = ctk.CTkFrame(left_frame, fg_color="#383838", corner_radius=0, height=28)
        dev_header.pack(fill="x")
        ctk.CTkLabel(dev_header, text="   DEVICES", font=ctk.CTkFont(size=11, weight="bold"), text_color="#cccccc", anchor="w").pack(side="left")
        
        self.dev_scroll = ctk.CTkScrollableFrame(left_frame, fg_color="transparent")
        self.dev_scroll.pack(fill="both", expand=True, padx=3, pady=3)
        for i, dev in enumerate(self.devices):
            m = DeviceMonitorWidget(self, dev, i+1)
            m.pack(fill="x", pady=1)
            self.device_monitors[dev] = m
        
        # Right: Heroes
        right_frame = ctk.CTkFrame(main_frame, fg_color="#2b2b2b", corner_radius=8)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        
        hero_header = ctk.CTkFrame(right_frame, fg_color="#383838", corner_radius=0, height=56)
        hero_header.pack(fill="x")
        hero_header.pack_propagate(False)
        
        title_row = ctk.CTkFrame(hero_header, fg_color="transparent", height=28)
        title_row.pack(fill="x")
        ctk.CTkLabel(title_row, text="   🏆 HEROES FOUND", font=ctk.CTkFont(size=11, weight="bold"), text_color="#f2c94c", anchor="w").pack(side="left")
        self.lbl_filter_count = ctk.CTkLabel(title_row, text="Filtered: 0", font=ctk.CTkFont(size=10), text_color="#aaaaaa")
        self.lbl_filter_count.pack(side="right", padx=10)
        
        # Filter Entry
        filter_frame = ctk.CTkFrame(hero_header, fg_color="transparent", height=24)
        filter_frame.pack(fill="x", padx=5, pady=2)
        self.ent_filter = ctk.CTkEntry(filter_frame, placeholder_text="🔍 Search heroes or gear (e.g. lapel)...", font=ctk.CTkFont(size=11), height=22, fg_color="#1e1e1e", border_width=1)
        self.ent_filter.pack(fill="x", expand=True)
        self.ent_filter.bind("<KeyRelease>", lambda e: self.on_filter_changed())
        
        self.hero_scroll = ctk.CTkScrollableFrame(right_frame, fg_color="transparent")
        self.hero_scroll.pack(fill="both", expand=True, padx=3, pady=3)
        
        # 3. LOG AREA
        log_frame = ctk.CTkFrame(self, fg_color="#1e1e1e", corner_radius=6, height=80)
        log_frame.pack(fill="x", padx=6, pady=(0, 4))
        log_frame.pack_propagate(False)
        
        self.log_text = ctk.CTkTextbox(log_frame, font=ctk.CTkFont(family="Consolas", size=10), text_color="#8b949e", fg_color="#1e1e1e")
        self.log_text.pack(fill="both", expand=True, padx=2, pady=2)
        self.log_text.configure(state="disabled")
        
        self.log("SYSTEM", "GUI started. Please press [START ALL] to begin.")
        
        # 4. BOTTOM BAR
        bottom_bar = ctk.CTkFrame(self, height=32, fg_color="#333333", corner_radius=0)
        bottom_bar.pack(fill="x")
        
        base_path = os.path.dirname(os.path.abspath(__file__))
        backup_folder = os.path.join(base_path, "backup")
        heroes_folder = os.path.join(base_path, "backup-id")
        
        ctk.CTkButton(bottom_bar, text="🔌 Connect Missing", width=85, height=22, font=ctk.CTkFont(size=10), fg_color="#4caf50", command=self.connect_missing_devices).pack(side="left", padx=3, pady=4)
        ctk.CTkButton(bottom_bar, text="⚙ Config", width=70, height=22, font=ctk.CTkFont(size=10), fg_color="#555555", command=self.open_config).pack(side="left", padx=3, pady=4)
        ctk.CTkButton(bottom_bar, text="📁 Backup", width=70, height=22, font=ctk.CTkFont(size=10), fg_color="#555555", command=lambda: subprocess.Popen(f'explorer "{backup_folder}"')).pack(side="left", padx=3, pady=4)
        ctk.CTkLabel(bottom_bar, text="v3.2.0", font=ctk.CTkFont(size=10), text_color="#888888").pack(side="right", padx=8)

    def open_device_log(self, device_id):
        # ✅ Open Notepad showing the actual log file
        safe_id = device_id.replace(":", "_").replace(".", "_")
        log_path = os.path.join(self.log_dir, f"device_{safe_id}.txt")
        
        # Ensure file exists so Notepad doesn't error out
        if not os.path.exists(log_path):
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"--- Log started for {device_id} ---\n")
        
        try:
            subprocess.Popen(["notepad.exe", log_path])
        except Exception as e:
            self.log("ERROR", f"Failed to open Notepad: {e}")

    def log_to_device(self, device_id, message):
        ts = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{ts}] {message}\n"
        if device_id not in self.device_logs:
            self.device_logs[device_id] = ""
        self.device_logs[device_id] += full_msg

        # ✅ Save to file
        safe_id = device_id.replace(":", "_").replace(".", "_")
        log_path = os.path.join(self.log_dir, f"device_{safe_id}.txt")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(full_msg)
        except Exception as e:
            print(f"[LOG FILE ERROR] {e}")

        # Keep only last 1000 lines in memory
        lines = self.device_logs[device_id].split('\n')
        if len(lines) > 1000:
            self.device_logs[device_id] = '\n'.join(lines[-1000:])

        if "⚠️" in message or "❌" in message or "Finished" in message:
            self.log(device_id, message)

    def log(self, level, message):
        ts = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{ts}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", full_msg)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        # ✅ Save to system log file
        try:
            with open(self._system_log_path, "a", encoding="utf-8") as f:
                f.write(full_msg)
        except Exception as e:
            print(f"[SYSTEM LOG FILE ERROR] {e}")
            

    def connect_missing_devices(self):
        """Scan for missing adb connections and start them dynamically"""
        self.log("INFO", "Scanning for missing emulators...")
        # Automatically perform port scan before checking devices
        connect_known_ports()
        
        current_devices = get_connected_devices()
        emulator_devices = [d for d in current_devices if d.startswith("emulator-") or d.startswith("127.0.0.1:")]
        
        new_count = 0
        for dev in emulator_devices:
            if dev not in self.devices:
                new_count = new_count + 1
                self.devices.append(dev)
                # Add to UI
                m = DeviceMonitorWidget(self.dev_scroll, dev, len(self.devices))
                m.pack(fill="x", pady=1)
                self.device_monitors[dev] = m
                
                # Start bot thread
                if getattr(self, 'is_started', False) and not getattr(self.args, 'no_start', False):
                    bot = BotInstance(dev)
                    t = threading.Thread(target=bot.run_step1)
                    t.daemon = True
                    t.start()
                    self.bot_threads.append(t)
                self.log("SUCCESS", f"Connected new device: {dev}")
        
        if new_count > 0:
            self.lbl_status.configure(text=f"   ● ONLINE ({len(self.devices)})")
        else:
            self.log("INFO", "No new devices found.")

    def log(self, level, message): 
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _start_single_bot(self, device_id):
        bot = BotInstance(device_id)
        t = threading.Thread(target=bot.run_step1)
        t.daemon = True
        t.start()
        self.bot_threads.append(t)
        self.log("INFO", f"🚀 Started bot on {device_id}")

    def start_bot(self):
        if getattr(self, 'is_started', False):
            self.log("WARN", "Bot is already running.")
            return
        self.is_started = True
        if hasattr(self, 'btn_start'):
            self.btn_start.configure(state="disabled", fg_color="#555555", text="⏳ RUNNING")
        self.lbl_auto_start.configure(text="[ BOT IS RUNNING ]", text_color="#4caf50")
        
        try:
            with open("configmain.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except:
            cfg = {}
        delay_sec = cfg.get("thread_delay", 5)
        self.log("INFO", f"Starting Bot Threads (Delay: {delay_sec}s per device)...")
        
        for i, device_id in enumerate(self.devices):
            delay_ms = i * int(delay_sec) * 1000
            # Pass device_id explicitly by freezing the variable in the lambda
            self.after(delay_ms, lambda d=device_id: self._start_single_bot(d))

    def on_closing(self):
        if messagebox.askokcancel("Quit", "คุณต้องการหยุดบอทและปิดโปรแกรมใช่หรือไม่?\n(จะทำการ Kill ADB และ Python ทั้งหมด)"):
            print("[GUI] Shutting down... Killing background processes.")
            try:
                # Kill ADB and Python processes on Windows
                if os.name == 'nt':
                    subprocess.run("taskkill /F /IM adb.exe /T", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    # We don't kill python.exe here because it would kill THIS process too early.
                    # We use os._exit(0) at the end.
            except:
                pass
            self.destroy()
            os._exit(0)

    def update_realtime_stats(self):
        try:
            # Load shared stats from other processes
            ui_stats.load_shared()
            
            with ui_stats.lock:
                # Count files real-time in the backup folder
                source_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
                qsize = 0
                if os.path.exists(source_folder):
                    qsize = len([f for f in os.listdir(source_folder) if f.lower().endswith(".xml")])
                
                self.lbl_file_count.configure(text=f"📁 {qsize}")
                self.lbl_succ_count.configure(text=f"✅ {ui_stats.success_count}")
                self.lbl_fail_count.configure(text=f"❌ {ui_stats.fail_count}")
                self.lbl_random_fail.configure(text=f"🎲 {ui_stats.random_fail_count}")
                
                for dev, stat in ui_stats.device_statuses.items():
                    if dev in self.device_monitors:
                        self.device_monitors[dev].update_state(status=stat.get('status'), step=stat.get('step'))
                
                hero_data: dict = dict(ui_stats.get_hero_combo_stats())
                
                # Handle Login Failures (fixid x 8)
                login_fail_count = ui_stats.fail_count
                if login_fail_count > 0:
                    hero_data["❌ เข้าไม่ได้ (Login Failed)"] = login_fail_count
                
                # Handle Gacha Failures (swap_shop/gachaout)
                random_fail_count = ui_stats.random_fail_count
                # Also collect raw "สุ่มไม่ได้" from hero_found_list
                raw_random_fail = hero_data.pop("สุ่มไม่ได้", 0)
                total_gacha_fail = random_fail_count + raw_random_fail
                if total_gacha_fail > 0:
                    hero_data["❌ สุ่มไม่ได้"] = total_gacha_fail
                
                # 2. Handle "Success but No Hero/Gear Found"
                # Merge various 'Success' or 'Not Found' keys into one positive label
                not_found_count = (hero_data.pop("ไม่เจอ", 0) + 
                                   hero_data.pop("Not Found", 0) + 
                                   hero_data.pop("Success", 0))
                
                if not_found_count > 0:
                    hero_data["✅ สำเร็จ (Success)"] = not_found_count
                
                for hero, count in hero_data.items():
                    if hero not in self.hero_stats_labels:
                        # Color coding: Red for failures, Green for others
                        # Only Login Failed, Cannot Gacha, and Kaiby are real errors
                        is_error_row = "Login Failed" in hero or "สุ่มไม่ได้" in hero or "ไก่บี้" in hero
                        self.add_hero_row(hero, is_error_row)
                    
                    self.hero_stats_labels[hero].configure(text=str(count))
                
                # Explicitly hide rows based on conditions
                to_hide = ["ไม่เจอ", "❌ ไม่เจอ"]
                for old_key in to_hide:
                    if old_key in self.hero_rows:
                        self.hero_rows[old_key].pack_forget()
                
                # Update Filter
                self.filter_heroes()
                
                # Update Avg Time
                if ui_stats.login_time_count > 0:
                    avg_sec = ui_stats.total_login_time / ui_stats.login_time_count
                    if avg_sec >= 60:
                        self.lbl_avg_time.configure(text=f"Avg: {avg_sec/60:.1f}m")
                    else:
                        self.lbl_avg_time.configure(text=f"Avg: {avg_sec:.0f}s")
        except Exception as e:
            print(f"[GUI] Update error: {e}")
        
        self.after(2000, self.update_realtime_stats)

    def on_filter_changed(self):
        self.hero_filter_text = self.ent_filter.get().lower()
        self.filter_heroes()

    def filter_heroes(self):
        total_filtered: int = 0
        for hero, row in self.hero_rows.items():
            if not self.hero_filter_text or self.hero_filter_text in hero.lower():
                row.pack(fill="x", pady=1)
                # Get count from label text
                try:
                    count: int = int(self.hero_stats_labels[hero].cget("text"))
                    total_filtered += count
                except: pass
            else:
                row.pack_forget()
        
        if hasattr(self, 'lbl_filter_count'):
            self.lbl_filter_count.configure(text=f"Filtered: {total_filtered}")


    def add_hero_row(self, hero_name, is_not_found):
        bg = "#3d2020" if is_not_found else "#2a3a2a"
        txt_color = "#e53935" if is_not_found else "#4caf50"
        row = ctk.CTkFrame(self.hero_scroll, fg_color=bg, corner_radius=6, height=26)
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)
        ctk.CTkLabel(row, text=f"  {hero_name}", font=ctk.CTkFont(size=11, weight="bold"), text_color="white", anchor="w").pack(side="left", fill="x", expand=True)
        lbl_count = ctk.CTkLabel(row, text="0", font=ctk.CTkFont(size=12, weight="bold"), text_color=txt_color)
        lbl_count.pack(side="right", padx=8)
        self.hero_stats_labels[hero_name] = lbl_count
        self.hero_rows[hero_name] = row

    def open_config(self): MainConfigWindow(self)




def main():
    if not find_adb_executable():
        print("ADB not found.")
        return
    connect_known_ports()
    serials = get_connected_devices()
    if not serials:
        print("No devices found.")
        return

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

    global ui_stats
    ui_stats = SimpleUIStats()
    
    if GUI_AVAILABLE:
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        # Dummy args for GUI compatibility
        class Args: pass
        args = Args()
        args.no_start = False
        gui = ModernBotGUI(serials, args)
        gui.mainloop()
    else:
        print("GUI NOT AVAILABLE! Running in standard mode.")
        threads = []
        for serial in serials:
            bot = BotInstance(serial)
            t = threading.Thread(target=bot.run_step1)
            t.daemon = True
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

if __name__ == "__main__":
    main()
