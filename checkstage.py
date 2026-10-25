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

# Templates are re-read on every single match otherwise. Keyed by (path, mtime) so an
# autoupdate that swaps img/ mid-run still picks the new file up.
_TEMPLATE_CACHE = {}


def load_template(template_path):
    """cv2.imread with a cache. Returns None if the file is missing or unreadable."""
    try:
        mtime = os.path.getmtime(template_path)
    except OSError:
        return None
    key = (template_path, mtime)
    tpl = _TEMPLATE_CACHE.get(key)
    if tpl is None:
        tpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if tpl is None:
            return None
        _TEMPLATE_CACHE[key] = tpl
    return tpl


# No tap / no progress for this long -> clear app, reopen, run the RE-LOOP recovery
STALL_TIMEOUT_SEC = 18 * 60

# Gap between machine-gun tap bursts. Each burst already sends a full chain of taps in
# one adb call, so this paces process spawns, not tap rate. Lower = smoother in-game but
# far heavier on the host.
TAP_BURST_INTERVAL = 0.15


class StallDetected(Exception):
    """Raised when nothing has made progress for STALL_TIMEOUT_SEC."""
    pass


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

# =========================================================
# WireGuard แยกจอ (ระบบฉีดไฟล์ IP) - ยกมาจาก LGR/login.py
# ตั้งค่าใน configmain.json (wg_*) ; ไฟล์ .conf อยู่ใน wg/ , กุญแจอยู่ใน wg_accounts/<บัญชี>/
# =========================================================
_real_sleep = time.sleep


def _read_main_config():
    """อ่าน configmain.json ตรง ๆ (ใช้ตอนบูต ก่อนตัวแปร config หลักจะโหลด)"""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "configmain.json"), "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


config = _read_main_config()


def _wg_endpoint(conf):
    """host ของ Endpoint ในไฟล์ .conf (= IP ขาออกของ VPN)"""
    try:
        with open(conf, encoding="utf-8-sig") as f:
            for l in f:
                if l.strip().lower().startswith("endpoint"):
                    return l.split("=", 1)[1].strip().rsplit(":", 1)[0]
    except Exception:
        pass
    return os.path.basename(conf)


def _wg_generate_more(wg_dir, want, dev=""):
    """สร้างไฟล์เซิร์ฟเวอร์เพิ่มให้มี want ไฟล์ (ล็อกให้สร้างทีละจอ) - คืน True ถ้ามีไฟล์เพิ่มจริง"""
    os.makedirs(wg_dir, exist_ok=True)   # โฟลเดอร์หาย -> สร้างไฟล์ล็อกไม่ได้ แล้วเลิกเงียบ ๆ
    lock = os.path.join(wg_dir, ".gen.lock")
    try:
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > 300:
            os.remove(lock)
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except Exception:
        return False
    try:
        before = len(_wg_list_configs(wg_dir))
        managed = os.path.exists(os.path.join(wg_dir, ".managed"))
        kp = os.path.join(wg_dir, "keypair.txt")
        confs = _wg_list_configs(wg_dir)
        if managed and not os.path.exists(kp) and confs:
            with open(confs[0], "rb") as f_src, open(kp, "wb") as f_dst:
                f_dst.write(f_src.read())
        print(f"[{dev}] [WG] ไฟล์ VPN ว่างไม่พอ - สร้างเซิร์ฟเวอร์ใหม่เพิ่ม (มี {before} -> {want})")
        import wg_gen
        if managed:
            wg_gen.generate(want, config.get("wg_gen_countries") or None, wg_dir, 0, "", 0, managed_ok=True)
        else:
            wg_gen.generate(want, config.get("wg_gen_countries") or None, wg_dir,
                            int(config.get("wg_machine", 0) or 0),
                            str(config.get("wg_account", "") or "").strip() or None,
                            int(config.get("wg_total_machines", 30) or 30))
        return len(_wg_list_configs(wg_dir)) > before
    except Exception as e:
        print(f"[{dev}] [WG] สร้างไฟล์เพิ่มไม่สำเร็จ: {e}")
        return False
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _wg_replace_blocked(conf, dev=""):
    """เซิร์ฟเวอร์นี้โดนเกมบล็อก -> จดลง wg/.blocked.json (wg_gen ไม่สร้างซ้ำ), ลบไฟล์ทิ้ง,
    แล้วสั่ง wg_gen สร้างเซิร์ฟเวอร์ใหม่มาแทนให้จำนวนไฟล์เท่าเดิม
    (ข้ามถ้าไฟล์มาจาก server RemoteFileManager (.managed) ; หลายจอพร้อมกัน -> ล็อกให้สร้างทีละจอ)"""
    wg_dir = os.path.dirname(conf) or "."
    managed = os.path.exists(os.path.join(wg_dir, ".managed"))
    if not os.path.exists(conf) or (managed and not int(config.get("wg_regen_managed", 1) or 0)):
        return
    os.makedirs(wg_dir, exist_ok=True)   # โฟลเดอร์หาย -> สร้างไฟล์ล็อกไม่ได้ แล้วเลิกเงียบ ๆ
    lock = os.path.join(wg_dir, ".gen.lock")
    try:
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > 300:
            os.remove(lock)                 # ล็อกค้างจาก process ที่ตายไป
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except Exception:
        return                              # จออื่นกำลังสร้างอยู่ - รอบนั้นจะเติมให้ครบเอง
    try:
        ep = _wg_endpoint(conf)
        bp = os.path.join(wg_dir, ".blocked.json")
        try:
            with open(bp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d[ep] = time.time()
        with open(bp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        want = len(_wg_list_configs(wg_dir))
        kp = os.path.join(wg_dir, "keypair.txt")
        if managed and not os.path.exists(kp):
            # ไฟล์จาก server: เก็บกุญแจไว้ก่อนลบ -> ไฟล์ใน wg/ หมดเกลี้ยงก็ยังสร้างใหม่ได้
            with open(conf, "rb") as f_src, open(kp, "wb") as f_dst:
                f_dst.write(f_src.read())
        # ไฟล์ที่ออก IP เดียวกัน (แชร์เซิร์ฟเวอร์หลายจอ คนละกุญแจ) โดนบล็อกด้วยกันหมด -> ลบทุกตัว
        gone = [c for c in _wg_list_configs(wg_dir) if _wg_endpoint(c) == ep] or [conf]
        for c in gone:
            try:
                os.remove(c)
            except OSError:
                pass
        print(f"[{dev}] [WG] ลบ {', '.join(os.path.basename(c) for c in gone)} (เกมบล็อก IP {ep}) - สร้างเซิร์ฟเวอร์ใหม่มาแทน")
        import wg_gen
        if managed:
            # เครื่องที่รับไฟล์จาก server: ใช้กุญแจเดิมของเครื่อง (keypair.txt) สร้างเซิร์ฟเวอร์ใหม่ที่ยังไม่มีในเครื่อง
            wg_gen.generate(want, config.get("wg_gen_countries") or None, wg_dir, 0, "", 0, managed_ok=True)
        else:
            wg_gen.generate(want, config.get("wg_gen_countries") or None, wg_dir,
                            int(config.get("wg_machine", 0) or 0),
                            str(config.get("wg_account", "") or "").strip() or None,
                            int(config.get("wg_total_machines", 30) or 30))
    except Exception as e:
        print(f"[{dev}] [WG] สร้างไฟล์แทนไม่สำเร็จ: {e}")
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _wg_cooldown_load(wg_dir):
    """{endpoint: หมดเวลาพัก} ที่ยังไม่หมด (ใช้ร่วมทุกจอผ่านไฟล์ wg/.cooldown.json)"""
    try:
        with open(os.path.join(wg_dir, ".cooldown.json"), encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        return {k: v for k, v in d.items() if v > now}
    except Exception:
        return {}


def _wg_cooldown_add(wg_dir, endpoint):
    h = float(config.get("wg_cooldown_hours", 6) or 0)
    if h <= 0 or not endpoint:
        return
    d = _wg_cooldown_load(wg_dir)
    d[endpoint] = time.time() + h * 3600
    try:
        tmp = os.path.join(wg_dir, f".cooldown.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, os.path.join(wg_dir, ".cooldown.json"))
    except Exception:
        pass


def _wg_list_configs(wg_dir=None):
    """รายการไฟล์ config WireGuard ตามลำดับจอ (path ในเครื่อง)

    1) wg/wg.txt (Notepad ไฟล์เดียว) - วาง config ต่อกันได้หลายอัน แต่ละอันเริ่มด้วย [Interface]
       อันแรก = จอ 1, อันที่สอง = จอ 2 ... บรรทัดที่ขึ้นต้นด้วย # คือหมายเหตุ ไม่สนใจ
       (แยกเก็บเป็นไฟล์ไว้ใน wg/.split/ ให้บอทส่งเข้าเครื่อง)
    2) ตามด้วยไฟล์ *.conf ในโฟลเดอร์ wg/ เรียงตามชื่อ
    """
    if wg_dir is None:
        wg_dir = str(config.get("wg_dir", "wg"))
    out = []
    txt = os.path.join(wg_dir, "wg.txt")
    if os.path.exists(txt):
        try:
            with open(txt, "r", encoding="utf-8-sig", errors="replace") as f:
                lines = [ln.rstrip() for ln in f if not ln.lstrip().startswith(("#", "//"))]
            blocks, cur = [], []
            for ln in lines:
                if ln.strip().lower() == "[interface]" and cur:
                    blocks.append(cur)
                    cur = []
                cur.append(ln)
            if cur:
                blocks.append(cur)
            split_dir = os.path.join(wg_dir, ".split")
            os.makedirs(split_dir, exist_ok=True)
            for i, blk in enumerate(blocks, 1):
                text = "\n".join(blk).strip()
                if "privatekey" not in text.lower() or "endpoint" not in text.lower():
                    continue
                p = os.path.join(split_dir, f"wg{i:02d}.conf")
                old = open(p, encoding="utf-8").read() if os.path.exists(p) else None
                if old != text + "\n":
                    with open(p, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text + "\n")
                out.append(p)
        except Exception as e:
            print(f"[WG] อ่าน wg.txt ไม่ได้: {e}")
    try:
        out += [os.path.join(wg_dir, f) for f in sorted(os.listdir(wg_dir)) if f.lower().endswith(".conf")]
    except Exception:
        pass
    # ตัดไฟล์ที่ข้างในซ้ำกัน: กุญแจ (PrivateKey) เดียวกัน + เซิร์ฟเวอร์ (Endpoint) เดียวกัน
    # ต่อพร้อมกันหลายจอ = แย่งกันเน็ตหลุดทุกจอ (เช่นโหลด Singapore ซ้ำ ๆ ด้วย Key Pair เดิม) -> ใช้ได้แค่ไฟล์เดียว
    seen, uniq, dup = {}, [], []
    for p in out:
        try:
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                kv = {}
                for ln in f:
                    if "=" in ln:
                        k, v = ln.split("=", 1)
                        kv[k.strip().lower()] = v.strip()
            ident = (kv.get("privatekey", ""), kv.get("endpoint", "").rsplit(":", 1)[0].lower())
        except Exception:
            ident = (p, "")
        if ident in seen:
            dup.append((os.path.basename(p), os.path.basename(seen[ident])))
            continue
        seen[ident] = p
        uniq.append(p)
    if dup and not _wg_dup_warned:
        _wg_dup_warned.append(1)
        print(f"[WG] เตือน: มี {len(dup)} ไฟล์ซ้ำ (กุญแจ+เซิร์ฟเวอร์เดียวกัน ใช้พร้อมกันไม่ได้) - ไม่ใช้ไฟล์พวกนี้:")
        for a, b in dup:
            print(f"[WG]   {a}  ซ้ำกับ  {b}")
        print("[WG]   แก้: โหลดใหม่เป็น 'คนละเมือง' หรือเมืองเดิมแต่เลือก New Key Pair")
    return uniq


_wg_dup_warned = []   # เตือนไฟล์ซ้ำครั้งเดียวต่อ process


WG_APK_URL = "https://download.wireguard.com/android-client/com.wireguard.android-1.0.20260315.apk"
_wg_apk_lock = threading.Lock()


def _wg_apk_path():
    """ไฟล์ติดตั้งแอป WireGuard (Android) - ไม่มีจะโหลดมาเก็บที่ wg/wireguard.apk ครั้งเดียว (ทุกจอใช้ร่วม)"""
    wg_dir = str(config.get("wg_dir", "wg"))
    path = os.path.join(wg_dir, "wireguard.apk")
    with _wg_apk_lock:
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            return path
        try:
            import urllib.request
            os.makedirs(wg_dir, exist_ok=True)
            print("[WG] กำลังโหลดแอป WireGuard (ครั้งแรกครั้งเดียว)...")
            tmp = path + ".part"
            urllib.request.urlretrieve(str(config.get("wg_apk_url", WG_APK_URL)), tmp)
            os.replace(tmp, path)
            return path
        except Exception as e:
            print(f"[WG] โหลดแอป WireGuard ไม่ได้: {e}")
            return None


def reset_network_all(devices):
    """ก่อนเริ่มบอท: ล้างของค้างทุกจอ (พร้อมกัน) - tunnel WireGuard, แอป WireGuard, proxy
    แล้ว ping 1.1.1.1 จากในอีมูฯ บอกใน log ว่าจอไหนเน็ตใช้ได้/ไม่ได้
    ปิดได้ด้วย config "reset_network_on_start": 0
    """
    if not int(config.get("reset_network_on_start", 1) or 0):
        return
    pkg = "com.wireguard.android"
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def sh(dev, cmd, timeout=20):
        try:
            return subprocess.run([adb_path, "-s", dev, "shell", cmd], capture_output=True,
                                  text=True, errors="replace", timeout=timeout, **kw).stdout or ""
        except Exception:
            return ""

    def one(dev):
        if not int(config.get("minitouch", 0) or 0):
            # minitouch ค้างในอีมู = ระบบกันโกง (LIAPP) ขึ้น "Malicious tool(s) has been found" -> ฆ่า+ลบทิ้งทุกจอ
            sh(dev, "su -c 'pkill -9 -f minitouch; rm -f /data/local/tmp/minitouch'")
        had = bool(sh(dev, "ip -o link show 2>/dev/null | grep -E ' tun[0-9]+:' || true").strip())
        if int(config.get("wg_enabled", 1) or 0) and int(config.get("wg_once", 1) or 0):
            # wg_once: VPN ต้องเปิดแช่ไว้ - ตอนเริ่มห้ามปิด tunnel ที่เปิดอยู่ ล้างแค่ proxy ค้าง
            sh(dev, "settings put global http_proxy :0; settings delete global http_proxy; "
                    "settings delete global global_http_proxy_host; settings delete global global_http_proxy_port")
            print(f"[NET-RESET] {dev}: VPN {'เปิดอยู่แล้ว - ไม่ยุ่ง' if had else 'ปิดอยู่ - จะเปิดให้ก่อนเข้าเกม'}")
            return
        sh(dev, f"su -c 'am broadcast -f 0x20 -p {pkg} -a {pkg}.action.SET_TUNNEL_DOWN --es tunnel lgr'")
        time.sleep(1.5)
        sh(dev, f"su -c 'am force-stop {pkg}'")
        sh(dev, "settings put global http_proxy :0; settings delete global http_proxy; "
                "settings delete global global_http_proxy_host; settings delete global global_http_proxy_port")
        still = bool(sh(dev, "ip -o link show 2>/dev/null | grep -E ' tun[0-9]+:' || true").strip())
        ping = sh(dev, "ping -c 1 -W 3 1.1.1.1 >/dev/null 2>&1 && echo OK || echo FAIL", timeout=10).strip()
        state = ("ปิด VPN ค้างแล้ว" if had else "ไม่มี VPN ค้าง") + (" (แต่ tunnel ยังอยู่!)" if still else "")
        print(f"[NET-RESET] {dev}: {state} | เน็ต: {'ใช้ได้' if ping == 'OK' else 'ออกเน็ตไม่ได้!'}")

    print(f"[NET-RESET] ล้าง VPN/proxy ค้างทุกจอก่อนเริ่ม ({len(devices)} จอ)...")
    ts = [threading.Thread(target=one, args=(d,), daemon=True) for d in devices]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    wait = float(config.get("net_reset_wait", 30) or 0)
    if int(config.get("wg_enabled", 1) or 0) and int(config.get("wg_once", 1) or 0):
        wait = 0                     # wg_once: ไม่ได้ปิด VPN -> ไม่ต้องรอเน็ตนิ่ง
    if wait > 0:
        print(f"[NET-RESET] ปิด VPN ทุกจอแล้ว - รอเน็ตนิ่ง {wait:.0f} วิ ก่อนเริ่มทำงาน...")
        time.sleep(wait)



def _wg_import_keys_from_main():
    """play150 ยังไม่มีกุญแจ Windscribe (wg_accounts/ ไม่ขึ้น git) -> หาจากโฟลเดอร์ main (LGR) บนเครื่อง
    ค้นข้าง play150 และทั่ว Desktop ลึก 3 ชั้น: เอา wg_accounts/ ทั้งโฟลเดอร์ ; ไม่มีก็ใช้ .conf ใน wg/ 1 ไฟล์เป็นกุญแจ
    คืน True = มีกุญแจพร้อมใช้"""
    here = os.path.dirname(os.path.abspath(__file__))
    acc = os.path.join(here, "wg_accounts")
    if (glob.glob(os.path.join(acc, "*", "*.conf"))
            or glob.glob(os.path.join(here, str(config.get("wg_dir", "wg")), "*.conf"))):
        return True
    bases = [os.path.dirname(here), os.path.join(os.path.expanduser("~"), "Desktop")]
    seen, found_acc, found_wg = set(), [], []
    for b in bases:
        for depth in range(4):
            mid = [b] + ["*"] * depth
            for d in glob.glob(os.path.join(*mid, "wg_accounts")):
                d = os.path.abspath(d)
                if d not in seen and not d.startswith(here + os.sep) and glob.glob(os.path.join(d, "*", "*.conf")):
                    seen.add(d); found_acc.append(d)
            for c in glob.glob(os.path.join(*mid, "wg", "*.conf")):
                c = os.path.abspath(c)
                if not c.startswith(here + os.sep):
                    found_wg.append(c)
    if found_acc:
        shutil.copytree(found_acc[0], acc, dirs_exist_ok=True)
        print(f"[WG] ก๊อปกุญแจ Windscribe จาก {found_acc[0]} มาแล้ว")
        return True
    if found_wg:
        os.makedirs(os.path.join(acc, "_backup"), exist_ok=True)
        shutil.copy2(found_wg[0], os.path.join(acc, "_backup", "key.conf"))
        print(f"[WG] ใช้กุญแจจาก {found_wg[0]} (เก็บไว้ที่ wg_accounts/_backup/key.conf)")
        return True
    print(f"[WG] ไม่เจอกุญแจ Windscribe เลย (หาใน {', '.join(bases)} ลึก 3 ชั้น)")
    return False


_WG_NO_KEYS = False


def wg_startup(devices):
    """ก่อนเริ่มบอท: ล้างการจอง/รายชื่อบล็อกเก่า, ล้างเน็ตทุกจอ, สร้างไฟล์ IP ให้พอ, เปิดตัวเฝ้า wg/"""
    global _WG_NO_KEYS
    if int(config.get("wg_enabled", 1) or 0) and not _wg_import_keys_from_main():
        # ไม่มีกุญแจ = สร้างไฟล์ IP ไม่ได้แน่นอน -> ปิด VPN ทั้งรอบนี้ เล่นด้วยเน็ตปกติ (ไม่วนรีสตาร์ทไอดี)
        _WG_NO_KEYS = True
        config["wg_enabled"] = 0
        print("[WG] ปิด VPN รอบนี้ - เล่นด้วยเน็ตปกติ (วางไฟล์ .conf ไว้ที่ wg_accounts/account1/ แล้วเปิดบอทใหม่)")
    cleanup_count = 0
    # 2b. ล้างรายชื่อ IP ที่จดว่าโดนบล็อก/พักไว้จากรอบก่อน (wg_reset_blocked_on_start, default 1)
    #     กันรายชื่อที่จดผิดสะสมจนไม่มี IP ให้ใช้/สร้างใหม่ไม่ได้ - ตัวที่โดนจริงบอทจะทดสอบเจอใหม่เอง
    if int(config.get("wg_reset_blocked_on_start", 1) or 0):
        for _bf in (".blocked.json", ".cooldown.json"):
            try: os.remove(os.path.join(str(config.get("wg_dir", "wg")), _bf)); cleanup_count += 1
            except OSError: pass
    # 3. ล้างการจองไฟล์ WireGuard ของรอบก่อน (แต่ละจอจะจองใหม่ไม่ซ้ำกัน)
    for lf in glob.glob(os.path.join(str(config.get("wg_dir", "wg")), ".claims", "*.lock")):
        try: os.remove(lf); cleanup_count += 1
        except: pass
    if cleanup_count > 0:
        print(f"[CLEANUP] Removed {cleanup_count} stale WG file(s)")

    # === ล้างเน็ตทุกจอก่อนเริ่ม: ปิด VPN (WireGuard) ที่ค้าง + ล้าง proxy ค้าง แล้วเช็คว่าออกเน็ตได้ ===
    reset_network_all(devices)

    # === ไฟล์ WireGuard ไม่พอจำนวนจอ -> สร้างเพิ่มอัตโนมัติจาก Key Pair ที่มี (wg_gen.py) ===
    if int(config.get("wg_enabled", 1) or 0) and int(config.get("wg_auto_gen", 1) or 0):
        try:
            import wg_gen
            _wgd = str(config.get("wg_dir", "wg"))
            if not glob.glob(os.path.join(_wgd, "*.conf")):
                # ครั้งแรก/ไฟล์หมด: wg/ ว่าง -> สร้าง IP ก่อนเริ่มงานเสมอ (.managed ค้างจาก remote ทำให้ wg_gen ไม่ยอมสร้าง)
                os.makedirs(_wgd, exist_ok=True)
                try: os.remove(os.path.join(_wgd, ".managed"))
                except OSError: pass
                print("[WG-GEN] wg/ ยังไม่มีไฟล์ IP - สร้างให้ก่อนเริ่มบอท...")
            _mach = int(config.get("wg_machine", 0) or 0)   # เลขเครื่อง (หลายเครื่องใช้กุญแจเดียวกัน) 0 = ปิด
            _per = int(config.get("wg_per_machine", 0) or 0) or (len(devices) + int(config.get("wg_gen_spare", 2)))
            wg_gen.generate(_per, config.get("wg_gen_countries") or None, str(config.get("wg_dir", "wg")), _mach,
                            str(config.get("wg_account", "") or "").strip() or None,   # เลือกบัญชี: wg_accounts/<ชื่อ>/
                            int(config.get("wg_total_machines", 30) or 30))
        except Exception as e:
            print(f"[WG-GEN] สร้างไฟล์อัตโนมัติไม่สำเร็จ: {e}")

    # === สร้าง IP ใหม่ทุก ๆ wg_regen_every_min นาที (default 60): ลบไฟล์ .conf เก่าใน wg/ ทิ้ง แล้วสร้างชุดใหม่ตามที่ตั้งไว้ ===
    #     จอที่ต่อ VPN อยู่แล้วไม่โดนตัด (tunnel อยู่ในเครื่องแล้ว) - ชุดใหม่ใช้ตอนจอต้องเปลี่ยน IP ครั้งถัดไป ; 0 = ปิด
    def _wg_regen_loop(_per_default):
        import glob as _glob
        while True:
            mins = float(config.get("wg_regen_every_min", 0) or 0)
            if mins <= 0:
                return
            if os.path.exists(os.path.join(str(config.get("wg_dir", "wg")), ".managed")):
                print("[WG-GEN] ไฟล์ IP มาจาก server (.managed) - ไม่สร้าง/ลบเองตามรอบ")
                return
            time.sleep(mins * 60)
            try:
                import wg_gen, shutil as _sh, tempfile as _tf
                wg_dir = str(config.get("wg_dir", "wg"))
                per = int(config.get("wg_per_machine", 0) or 0) or _per_default
                # สร้างชุดใหม่ในโฟลเดอร์ชั่วคราวก่อน - ได้ไฟล์จริงแล้วค่อยสลับ ; สร้างไม่ได้ = เก็บชุดเดิมไว้ (เคยลบก่อนแล้ว wg/ ว่างทั้งเครื่อง)
                tmp = os.path.join(os.path.dirname(os.path.abspath(wg_dir)), "wg_new")   # ต้องอยู่ข้าง wg/ - wg_gen หา wg_accounts/ จากโฟลเดอร์แม่
                _sh.rmtree(tmp, ignore_errors=True)
                os.makedirs(tmp, exist_ok=True)
                for kf in _glob.glob(os.path.join(wg_dir, "*.txt")) + _glob.glob(os.path.join(wg_dir, "*.json")):
                    _sh.copy2(kf, tmp)
                print(f"[WG-GEN] ครบ {mins:.0f} นาที - สร้าง IP ชุดใหม่ {per} ไฟล์...")
                wg_gen.generate(per, config.get("wg_gen_countries") or None, tmp,
                                int(config.get("wg_machine", 0) or 0),
                                str(config.get("wg_account", "") or "").strip() or None,
                                int(config.get("wg_total_machines", 30) or 30))
                fresh = _glob.glob(os.path.join(tmp, "*.conf"))
                if not fresh:
                    print("[WG-GEN] สร้างชุดใหม่ไม่ได้ - เก็บ IP ชุดเดิมไว้ใช้ต่อ")
                else:
                    old = _glob.glob(os.path.join(wg_dir, "*.conf"))
                    for p in old:
                        try: os.remove(p)
                        except OSError: pass
                    for p in fresh:
                        _sh.copy2(p, wg_dir)
                    for p in _glob.glob(os.path.join(wg_dir, ".claims", "*.lock")) + [os.path.join(wg_dir, ".cooldown.json"), os.path.join(wg_dir, ".blocked.json")]:
                        try: os.remove(p)
                        except OSError: pass
                    print(f"[WG-GEN] เปลี่ยน IP ชุดใหม่แล้ว: เก่า {len(old)} -> ใหม่ {len(fresh)} ไฟล์")
                _sh.rmtree(tmp, ignore_errors=True)
            except Exception as e:
                print(f"[WG-GEN] สร้าง IP ใหม่ตามรอบไม่สำเร็จ: {e}")

    # === ตัวเฝ้า: wg/ ไม่มีไฟล์ IP เลย -> รัน wg_gen สร้างใหม่ทันที (เช็คทุก 30 วิ) ตามบัญชีใน wg_account ===
    def _wg_empty_watch(_per_default):
        import glob as _glob
        while True:
            try:
                wg_dir = str(config.get("wg_dir", "wg"))
                _have = _glob.glob(os.path.join(wg_dir, "*.conf"))
                _bk = os.path.join(os.path.dirname(os.path.abspath(wg_dir)), "wg_accounts", "_backup", "key.conf")
                if _have and not os.path.exists(_bk):
                    # เก็บกุญแจสำรองไว้นอก wg/ ตั้งแต่ตอนยังมีไฟล์ (ไฟล์จาก remote ก็มีกุญแจ) -> หมดเมื่อไหร่สร้างเองได้ ไม่ต้องกด
                    os.makedirs(os.path.dirname(_bk), exist_ok=True)
                    import shutil as _sh
                    _sh.copy2(_have[0], _bk)
                    print(f"[WG-GEN] เก็บกุญแจสำรองไว้แล้ว ({os.path.basename(_have[0])}) - IP หมดเมื่อไหร่บอทสร้างใหม่เอง")
                if not _have:
                    import wg_gen
                    os.makedirs(wg_dir, exist_ok=True)
                    try:
                        os.remove(os.path.join(wg_dir, ".managed"))   # ไฟล์ server หมดแล้ว -> ให้ wg_gen สร้างเองได้
                    except OSError:
                        pass
                    per = int(config.get("wg_per_machine", 0) or 0) or _per_default
                    acc = str(config.get("wg_account", "") or "").strip() or None
                    print(f"[WG-GEN] wg/ ไม่มีไฟล์ IP เลย - รัน wg_gen สร้างใหม่ {per} ไฟล์ (บัญชี {acc or '-'})...")
                    wg_gen.generate(per, config.get("wg_gen_countries") or None, wg_dir,
                                    int(config.get("wg_machine", 0) or 0), acc,
                                    int(config.get("wg_total_machines", 30) or 30))
                    print(f"[WG-GEN] ตอนนี้มี {len(_glob.glob(os.path.join(wg_dir, '*.conf')))} ไฟล์ใน wg/")
            except Exception as e:
                print(f"[WG-GEN] สร้าง IP ใหม่ไม่สำเร็จ: {e}")
            time.sleep(30)

    if int(config.get("wg_enabled", 1) or 0):
        threading.Thread(target=_wg_empty_watch, args=(len(devices) + int(config.get("wg_gen_spare", 2)),),
                         daemon=True).start()

    if int(config.get("wg_enabled", 1) or 0) and float(config.get("wg_regen_every_min", 0) or 0) > 0:
        threading.Thread(target=_wg_regen_loop, args=(len(devices) + int(config.get("wg_gen_spare", 2)),),
                         daemon=True).start()


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
        self._in_reloop = False
        self._login_fixid_count = 0
        self.login_done = False
        self._root_mode = None  # "su" | "adbd" | None (detected lazily)


    def _ensure_root(self):
        """Detect how to run root commands on this device.
        Order: adbd already root -> su binary -> `adb root` (emulator / debuggable build).
        Result cached in self._root_mode."""
        if self._root_mode:
            return self._root_mode

        def shell_out(*args, timeout=10):
            try:
                r = subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", *args],
                                   capture_output=True, text=True, timeout=timeout, **self.kwargs)
                return (r.stdout or "").strip()
            except Exception:
                return ""

        if shell_out("id", "-u") == "0":
            self._root_mode = "adbd"
        elif shell_out("su", "-c", "id -u") == "0":
            self._root_mode = "su"
        else:
            self.log("No 'su' on device. Trying 'adb root'...")
            try:
                subprocess.run([self.adb_cmd, "-s", self.device_id, "root"],
                               capture_output=True, text=True, timeout=15, **self.kwargs)
            except Exception:
                pass
            for _ in range(10):
                time.sleep(1)
                if shell_out("id", "-u") == "0":
                    self._root_mode = "adbd"
                    break

        if self._root_mode:
            self.log(f"Root access OK (mode: {self._root_mode})")
        else:
            self.log("ERROR: No root access (no su and 'adb root' failed). Injection cannot work on this device!")
        return self._root_mode

    def root_shell(self, cmd, timeout=20):
        """Run a shell command as root using whichever mode is available.
        Returns CompletedProcess (stdout/stderr as text)."""
        mode = self._ensure_root()
        if mode == "su":
            args = [self.adb_cmd, "-s", self.device_id, "shell", "su", "-c", cmd]
        else:
            args = [self.adb_cmd, "-s", self.device_id, "shell", cmd]
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **self.kwargs)
        except Exception as e:
            return subprocess.CompletedProcess(args, 1, "", str(e))

    def log(self, message):
        print(f"[{self.device_id}] {message}")
        if GUI_INSTANCE:
            GUI_INSTANCE.log_to_device(self.device_id, message)

    def load_config(self):

        try:
            with open("configmain.json", "r") as f:
                self.config = json.load(f)
            config.update(self.config)
            if _WG_NO_KEYS:
                config["wg_enabled"] = 0
        except:
            self.config = {"getclearquest": 0}

    def push_file(self, local_xml_path):
        remote_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        self.log(f"Injecting file: {local_xml_path} (Robust Mode)...")

        if not os.path.exists(local_xml_path):
            self.log(f"✗ Local file not found: {local_xml_path}")
            return False

        if not self._ensure_root():
            self.log(f"✗ Injection FAILED: no root access on {self.device_id}")
            return False

        final_dir = "/data/data/com.linecorp.LGRGS/shared_prefs"
        final = remote_path

        # Ensure directories exist
        self.root_shell(f"mkdir -p {final_dir}")

        # Stop app first
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
        time.sleep(2)

        # Kill for certainty
        self.root_shell("killall -9 com.linecorp.LGRGS 2>/dev/null || true")
        time.sleep(1)

        src = os.path.abspath(local_xml_path)
        tmp = f"/data/local/tmp/temp_pref_{self.device_id.replace(':','_')}.xml"

        import hashlib
        with open(src, "rb") as f:
            local_md5 = hashlib.md5(f.read()).hexdigest()

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # 1. Push to temp location (shell-writable)
                result = subprocess.run([self.adb_cmd, "-s", self.device_id, "push", src, tmp],
                                        capture_output=True, text=True, timeout=45, **self.kwargs)
                if result.returncode != 0:
                    self.log(f"Push attempt {attempt} failed: {(result.stderr or result.stdout).strip()}")
                    time.sleep(2)
                    continue

                # 2. Copy into app dir as root, fix owner/perms/label
                shell_cmd = (
                    f"rm -f {final}; "
                    f"cp {tmp} {final} && "
                    f"chown $(stat -c %u:%g {final_dir}) {final}; "
                    f"chmod 660 {final}; "
                    f"restorecon {final} 2>/dev/null; "
                    f"rm -f {tmp}; sync"
                )
                r = self.root_shell(shell_cmd)

                # 3. Verify by checksum
                v = self.root_shell(f"md5sum {final}")
                remote_md5 = (v.stdout or "").strip().split(" ")[0] if v.stdout else ""
                if remote_md5 == local_md5:
                    self.log(f"✓ Injection successful on attempt {attempt} (md5 verified, mode: {self._root_mode})")
                    return True

                err = ((r.stdout or "") + (r.stderr or "") + (v.stdout or "") + (v.stderr or "")).strip()
                self.log(f"Inject attempt {attempt}: verify failed (device md5={remote_md5 or 'none'}, local={local_md5}) {err[:200]}")
                time.sleep(2)

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
            self.root_shell(f"cp {src_remote} {temp_remote}")
            
            # 2. Set permissions so adb pull can read it
            self.root_shell(f"chmod 666 {temp_remote}")
            
            # 3. Pull from temp location
            self.log(f"Pulling {src_remote} -> {local_path} (via {temp_remote})...")
            result = subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", temp_remote, local_path], 
                                   capture_output=True, text=True, **self.kwargs)
            
            if result.returncode == 0:
                self.log(f"✓ File pulled successfully to {local_path}")
            else:
                self.log(f"✗ Pull failed: {result.stderr}")
            
            # 4. Clean up temp file
            self.root_shell(f"rm -f {temp_remote}")
                           
        except Exception as e:
            self.log(f"Pull file error: {e}")
            # Fallback: try direct pull with chmod
            try:
                self.root_shell(f"chmod 666 {src_remote}")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", src_remote, local_path], **self.kwargs)
                self.log(f"Fallback pull completed.")
            except Exception as e2:
                self.log(f"Fallback pull also failed: {e2}")

    def adb_run(self, cmd_list, timeout=15):
        """Helper to run adb commands with uniform settings"""
        return subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout, **self.kwargs)

    # ===== WireGuard แยกจอ (ยกมาจาก LGR/login.py) =====
    WG_PKG = "com.wireguard.android"
    WG_TUNNEL = "lgr"

    def _wg_conf_for_device(self):
        """ไฟล์ .conf ของจอนี้ (None = ไม่มีไฟล์/ไม่เปิดใช้)"""
        wg_dir = str(config.get("wg_dir", "wg"))
        mapped = (config.get("wg_per_device") or {}).get(self.device_id)
        if mapped:
            p = mapped if os.path.isabs(mapped) else os.path.join(wg_dir, mapped)
            return p if os.path.exists(p) else None
        confs = _wg_list_configs(wg_dir)
        if (not confs and int(config.get("wg_regen_blocked", 1) or 0)
                and time.time() - getattr(self, "_wg_empty_gen_t", 0) > 60):
            # ไม่มีไฟล์ IP เลย (ถูกลบ/หาย) -> สร้างใหม่เองทันที (โหมดบัญชีไม่มี keypair.txt ก็สร้างได้)
            self._wg_empty_gen_t = time.time()
            # ไฟล์ใน wg/ หมด (ลบตัวที่โดนบล็อกไปหมด) แต่ยังมีกุญแจ -> สร้างชุดใหม่
            _wg_generate_more(wg_dir, int(config.get("wg_gen_extra", 5)) + 10, self.device_id)
            confs = _wg_list_configs(wg_dir)
        if not confs:
            return None
        # แต่ละจอ "จอง" ไฟล์ของตัวเอง ไม่ใช้ซ้ำกับจออื่น (กุญแจเดียวกันต่อพร้อมกัน = ตีกันเน็ตหลุด)
        # ลองไฟล์ตามลำดับจอ MuMu ก่อน (พอร์ต 16384 + 32*i) จะได้ไฟล์เดิมทุกครั้งที่เปิดบอต
        try:
            port = int(self.device_id.rsplit(":", 1)[1])
            idx = (port - 16384) // 32 if port >= 16384 else port
        except Exception:
            idx = sum(map(ord, self.device_id))
        # แต่ละจอเป็นคนละ process -> จองด้วยไฟล์ล็อก wg/.claims/<ชื่อไฟล์>.lock (สร้างแบบ O_EXCL = ได้คนเดียว)
        claim_dir = os.path.join(wg_dir, ".claims")
        os.makedirs(claim_dir, exist_ok=True)

        def _owner(p):
            try:
                with open(os.path.join(claim_dir, os.path.basename(p) + ".lock"), encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                return None

        bad = set(getattr(self, "_wg_bad", set()))   # ไฟล์ที่เน็ตหลุดซ้ำบนจอนี้ (auto failover) - ไม่กลับไปใช้
        # เซิร์ฟเวอร์ (IP ขาออก) ที่จอไหนก็ตามเพิ่งโดน block -> พักไว้ wg_cooldown_hours ชม. ทุกจอเลี่ยง
        cool = {} if getattr(self, "_wg_ignore_cool", False) else _wg_cooldown_load(wg_dir)
        bad |= {c for c in confs if _wg_endpoint(c) in cool}
        # จำกัดจำนวนจอต่อเซิร์ฟเวอร์เดียวกัน (IP ขาออกเดียวกัน) ; 0 = ไม่จำกัด
        max_ip = int(config.get("wg_max_per_ip", 0) or 0)
        if max_ip > 0:
            used = {}
            for c in confs:
                o = _owner(c)
                if o and o != self.device_id:
                    e = _wg_endpoint(c)
                    used[e] = used.get(e, 0) + 1
            full = {c for c in confs if used.get(_wg_endpoint(c), 0) >= max_ip}
            if len(full | bad) < len(confs):
                bad |= full
        for p in confs:                      # จองไว้แล้วจากรอบก่อน -> ใช้อันเดิม
            if _owner(p) == self.device_id and p not in bad:
                return p
        order = confs[idx % len(confs):] + confs[:idx % len(confs)]
        for p in order:
            if p in bad:
                continue
            lock = os.path.join(claim_dir, os.path.basename(p) + ".lock")
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            except Exception:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.device_id)
            return p
        if (not getattr(self, "_wg_gen_tried", False) and int(config.get("wg_regen_blocked", 1) or 0)
                and time.time() - getattr(self, "_wg_gen_last_t", 0) > 600):   # สร้างไม่ได้แล้ว อย่าลองซ้ำทุกรอบ (เสียเวลาโหลดรายชื่อเซิร์ฟเวอร์)
            self._wg_gen_last_t = time.time()
            # ไม่มีไฟล์ว่างให้ใช้เลย (โดนบล็อก/พัก/จองเต็ม) -> สร้างเซิร์ฟเวอร์ใหม่เพิ่มแล้วลองอีกรอบ (ไม่ปล่อยจอไปใช้เน็ตบ้าน)
            self._wg_gen_tried = True
            try:
                if _wg_generate_more(wg_dir, len(confs) + int(config.get("wg_gen_extra", 5)), self.device_id):
                    return self._wg_conf_for_device()
            finally:
                self._wg_gen_tried = False
        if bad and len(bad) >= len(confs):
            # ลองครบทุกไฟล์แล้ว -> ให้โอกาสใหม่ทั้งหมด (วนรอบใหม่) แทนที่จะไม่มี VPN
            print(f"[{self.device_id}] [WG] ลองครบทุกเซิร์ฟเวอร์แล้ว - เริ่มวนใหม่")
            if not getattr(self, "_wg_bad", None):
                # ทุกตัวติดพัก -> ยอมใช้ตัวที่ติดพักไปก่อน ดีกว่าไม่มี VPN เลย (wg_required ห้ามเข้าเกมด้วยเน็ตบ้าน)
                if getattr(self, "_wg_ignore_cool", False):
                    if int(config.get("wg_lock", 1) or 0) and confs:
                        # wg_lock: ไม่มีไฟล์ว่าง -> ห้ามปล่อยจอไม่มี IP: ใช้ IP เดิมของจอ ถ้าไม่มีก็ใช้ไฟล์ร่วมกับจออื่น
                        last = getattr(self, "_wg_last_conf", None)
                        pick = last if last and os.path.isfile(last) else confs[idx % len(confs)]
                        if not getattr(self, "_wg_share_warned", False):
                            self._wg_share_warned = True
                            print(f"[{self.device_id}] [WG] ไฟล์ว่างไม่พอ - ใช้ {os.path.basename(pick)} ร่วมกับจออื่นไปก่อน (ไม่ปล่อยให้ไม่มี IP)")
                        return pick
                    return None              # ไม่สนการพักแล้วก็ยังไม่ว่าง = ไฟล์ถูกจออื่นจองหมดจริง
                self._wg_ignore_cool = True
                try:
                    return self._wg_conf_for_device()
                finally:
                    self._wg_ignore_cool = False
            self._wg_bad = set()
            return self._wg_conf_for_device()
        if not getattr(self, "_wg_short_warned", False):
            self._wg_short_warned = True
            print(f"[{self.device_id}] [WG] ไฟล์ config ไม่พอ ({len(confs)} ไฟล์ ถูกจออื่นจองหมดแล้ว) - จอนี้ไม่ต่อ VPN "
                  f"(โหลด .conf เพิ่มมาใส่ {wg_dir}/)")
        if int(config.get("wg_lock", 1) or 0) and confs:
            # wg_lock: ไม่มีไฟล์ว่าง -> ห้ามปล่อยจอไม่มี IP: ใช้ IP เดิมของจอ ถ้าไม่มีก็ใช้ไฟล์ร่วมกับจออื่น
            last = getattr(self, "_wg_last_conf", None)
            pick = last if last and os.path.isfile(last) else confs[idx % len(confs)]
            if not getattr(self, "_wg_share_warned", False):
                self._wg_share_warned = True
                print(f"[{self.device_id}] [WG] ไฟล์ว่างไม่พอ - ใช้ {os.path.basename(pick)} ร่วมกับจออื่นไปก่อน (ไม่ปล่อยให้ไม่มี IP)")
            return pick
        return None

    def _wg_after_refresh(self):
        """wg_after_login: กด refresh แล้ว -> ต่อ VPN ตรงนี้เลย (ส่งไฟล์/เปิดเกมไปด้วยเน็ตปกติแล้ว)"""
        if int(config.get("wg_after_login", 0) or 0):
            self._ensure_wireguard()

    def _wg_vpn_active(self):
        """Android ยังมี VPN ต่ออยู่ไหม (ดูทั้ง tun interface และ VPN network ใน connectivity)"""
        if self._wg_is_up():
            return True
        r = self.adb_shell("dumpsys connectivity 2>/dev/null | grep -c 'VPN CONNECTED' || true", timeout=15)
        try:
            return int((r.stdout or b"0").strip().splitlines()[0] or 0) > 0
        except Exception:
            return False

    def _wg_down(self, force=False):
        """ปิด VPN ของจอนี้ "สนิท" ก่อนส่งไฟล์/เปิดเกม ให้ออกเน็ตด้วย IP ปกติ

        ทำทุกครั้ง (ไม่ใช่เฉพาะตอนเห็น tun): สั่งปิด tunnel -> ฆ่าแอป WireGuard ->
        รอจน Android ไม่มี VPN ค้าง -> รอจนออกเน็ตได้จริง (ping) -> พักให้เน็ตนิ่ง แล้วค่อยไปต่อ
        """
        if int(config.get("wg_lock", 1) or 0) and not force:
            return None   # wg_lock: ห้ามปิด VPN เด็ดขาด (ปิดแล้วเข้าเกมไม่ได้)
        if not int(config.get("wg_enabled", 1) or 0):
            return
        try:
            was = self._wg_vpn_active()
            self.adb_shell(f"su -c 'am broadcast -f 0x20 -p {self.WG_PKG} "
                           f"-a {self.WG_PKG}.action.SET_TUNNEL_DOWN --es tunnel {self.WG_TUNNEL}'", timeout=20)
            _real_sleep(1)
            self.adb_shell(f"su -c 'am force-stop {self.WG_PKG}'", timeout=15)
            self._wg_conf_applied = None
            gone = False
            for _ in range(10):
                if not self._wg_vpn_active():
                    gone = True
                    break
                _real_sleep(1)
            net = False
            for _ in range(10):
                r = self.adb_shell("ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 && echo OK || echo FAIL", timeout=8)
                if b"OK" in (r.stdout or b""):
                    net = True
                    break
                _real_sleep(1)
            if was or not gone or not net:
                _real_sleep(float(config.get("wg_down_settle", 0.5)))   # เน็ตเพิ่งสลับ - พักให้นิ่งก่อนเปิดเกม
                print(f"[{self.device_id}] [WG] ปิด VPN ก่อนเปิดเกม: "
                      f"{'ปิดสนิทแล้ว' if gone else 'ยังเห็น VPN ค้าง!'} | เน็ต: {'ใช้ได้' if net else 'ยังออกไม่ได้!'}")
        except Exception as e:
            print(f"[{self.device_id}] [WG] ปิด VPN ไม่สำเร็จ: {e}")

    def _wg_request_net_recover(self, where, switch_now=False, force_switch=False):
        """เจอ Unstable network (fixnetv3) บนจอที่ต่อ VPN อยู่ -> ทำทันทีตรงนี้ (ไม่ต้องรอลูปล็อกอิน):
        ปิดเกม -> ปิด VPN สนิท -> เปิด VPN ใหม่ -> เปิดเกมใหม่
        ไม่ล้าง shared_prefs (ไฟล์บัญชีที่ส่งเข้าไปยังอยู่ เปิดเกมแล้วเป็นไอดีเดิม)
        คืน True = จัดการแล้ว (ไม่ต้องกด RETRY) ; จอที่ไม่ได้ต่อ VPN / ครบโควตา -> False (กด RETRY แบบเดิม)"""
        if int(config.get("wg_lock", 1) or 0) and not force_switch:
            return False   # wg_lock: ไม่ปิด/สลับ VPN ตอนเน็ตหลุด -> กด RETRY แบบเดิม
        if not int(config.get("wg_enabled", 1) or 0):
            return False
        if int(config.get("wg_once", 1) or 0) and not force_switch:
            # wg_once: เน็ตแกว่งครั้งเดียวไม่เปลี่ยน IP (กด RETRY) แต่ถ้าเจอซ้ำครบ wg_net_switch_after ครั้ง
            # = IP นี้ใช้ไม่ได้แล้ว -> เปลี่ยน IP ทันที (VPN ปิดแค่ช่วงสลับ แล้วต่อตัวใหม่ก่อนเปิดเกมเสมอ)
            self._wg_net_hits = getattr(self, "_wg_net_hits", 0) + 1
            lim = int(config.get("wg_net_switch_after", 1) or 1)   # 1 = เน็ตหลุดครั้งแรกก็ ปิดแอป -> IP ใหม่ -> เปิดเกมใหม่
            if self._wg_net_hits < lim:
                # ยังไม่ครบ -> ปิดแอปแล้วเปิดใหม่ด้วย IP เดิมก่อน (VPN ไม่แตะ เปิดค้างไว้)
                print(f"[{self.device_id}] [WG] เน็ตหลุด ({where}) {self._wg_net_hits}/{lim} - ปิดแอปเข้าใหม่ (IP เดิม) ครบแล้วจะเปลี่ยน IP")
                try:
                    self.last_activity_time = time.time()
                    self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
                    _real_sleep(2)
                    self._wg_must_be_up()     # VPN ยังต่ออยู่ = ข้ามเร็ว ; หลุด = ต่อใหม่ก่อนเปิดเกม
                    self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "start", "-n", self._resolve_game_activity()], timeout=15)
                    self.last_activity_time = time.time()
                    _real_sleep(3)
                    return True
                except Exception as e:
                    print(f"[{self.device_id}] [WG] ปิดแอปเข้าใหม่ไม่สำเร็จ: {e}")
                    return False
            self._wg_net_hits = 0
            switch_now = True
        if force_switch:
            self._wg_net_hits = 0
            switch_now = True
        if not getattr(self, "_wg_conf_applied", None):
            return False                      # จอนี้ไม่ได้ต่อ VPN -> ไม่ใช่เรื่อง VPN
        lock = self.__dict__.setdefault("_wg_recover_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return True                       # อีกเธรดกำลังกู้อยู่ -> ไม่ต้องกดอะไร
        try:
            n = getattr(self, "_wg_recover_n", 0)
            if n >= int(config.get("wg_net_recover_max", 10)):
                return False                  # ครบโควตาแล้ว -> กลับไปกด RETRY แบบเดิม
            self._wg_recover_n = n + 1
            print(f"[{self.device_id}] [WG] เจอ Unstable network ({where}) - ปิดเกม + สลับ IP + เปิดเกมใหม่ "
                  f"(ครั้งที่ {n + 1})")
            self.last_activity_time = time.time()
            self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
            if force_switch:
                print(f"[{self.device_id}] [NET] ล้าง Android system proxy ก่อนปิด VPN", flush=True)
                self._clear_proxy_for_device()
            self._wg_down(force=force_switch)
            if switch_now:
                # fixnet1: เปลี่ยน IP ทันที (ไม่รอนับ 2 ครั้ง) - พักเซิร์ฟเวอร์นี้ไว้ แต่ไม่ลบไฟล์
                cur = getattr(self, "_wg_last_conf", None)
                if cur:
                    self.__dict__.setdefault("_wg_bad", set()).add(cur)
                    if not str(where).startswith("fixnet"):
                        # เน็ตหลุด (fixnet) ไม่ใช่โดนบล็อก -> ไม่พักเซิร์ฟเวอร์ 6 ชม. (เคยพักจนหมดทุกตัว ไม่เหลือให้ต่อ)
                        _wg_cooldown_add(os.path.dirname(cur), _wg_endpoint(cur))
                    try:
                        os.remove(os.path.join(os.path.dirname(cur), ".claims", os.path.basename(cur) + ".lock"))
                    except OSError:
                        pass
                    self._wg_conf_written = None
                    print(f"[{self.device_id}] [WG] {where}: เลิกใช้ {os.path.basename(cur)} - เปลี่ยน IP")
            else:
                # เน็ตหลุดซ้ำบนเซิร์ฟเวอร์เดิม -> สลับไปเซิร์ฟเวอร์สำรองที่ว่างอยู่ (auto failover)
                self._wg_strike("Unstable network")
            self._wg_must_be_up()
            self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "start", "-n",
                          self._resolve_game_activity()], timeout=15)
            self.last_activity_time = time.time()
            print(f"[{self.device_id}] [WG] เปิดเกมใหม่แล้ว (VPN: {os.path.basename(str(getattr(self, '_wg_conf_applied', '') or '-'))})")
            _real_sleep(3)
            return True
        except Exception as e:
            print(f"[{self.device_id}] [WG] กู้เน็ตไม่สำเร็จ: {e}")
            return False
        finally:
            lock.release()

    def _wg_auth_failed(self):
        """Authentication failed (fixid) ตอนต่อ VPN -> ด่าน login ของ LINE ไม่รับ IP ของ VPN
        เจอครบ wg_auth_fallback ไอดี (default 2) -> ปิด VPN ของจอนี้ไปจนปิดบอท ใช้เน็ตบ้านแทน (0 = ไม่ปิด)"""
        if int(config.get("wg_lock", 1) or 0):
            return None   # wg_lock: ไม่ปิด VPN แม้ login ไม่ผ่าน
        if not int(config.get("wg_enabled", 1) or 0) or not getattr(self, "_wg_conf_applied", None):
            return
        lim = int(config.get("wg_auth_fallback", 0) or 0)
        if lim <= 0:
            return
        self._wg_auth_fail = getattr(self, "_wg_auth_fail", 0) + 1
        print(f"[{self.device_id}] [WG] Authentication failed ผ่าน VPN ({self._wg_auth_fail}/{lim})")
        if self._wg_auth_fail >= lim:
            self._wg_off = True
            self._wg_down()
            print(f"[{self.device_id}] [WG] ด่าน login LINE ไม่รับ IP VPN - ปิด VPN จอนี้ ใช้เน็ตบ้านแทน (จนกว่าจะเปิดบอทใหม่)")

    def _wg_strike(self, reason, now_switch=False):
        """จอนี้เข้าเกมไม่ได้บนเซิร์ฟเวอร์ VPN ปัจจุบัน 1 ครั้ง (Unstable network / fixid ครบ / LINE GAME ค้าง /
        จอดำ / login failed) - ครบ wg_failover_after (2) ครั้งบนเซิร์ฟเวอร์เดิม -> เลิกใช้ แล้วจองเซิร์ฟเวอร์อื่น
        (มีผลตอนเปิดเกมครั้งถัดไป: open_app จะต่อ VPN ตัวใหม่ให้เอง) ; คืน True = สลับแล้ว"""
        if int(config.get("wg_lock", 1) or 0):
            # wg_lock: ใช้ IP เดิมต่อไป - ยกเว้นค้างหน้า LINE GAME ซ้ำ (LINE ไม่รับ IP นี้ = ไม่มีทางเข้าเกมได้)
            if not any(k in str(reason) for k in ("LINE GAME", "fixid", "login failed")):   # fixid/login failed ครบ = LINE ไม่รับ IP นี้ -> สลับด้วย
                return False
            self._wg_line_stuck = getattr(self, "_wg_line_stuck", 0) + 1
            lim = int(config.get("wg_line_stuck_switch", 2) or 2)
            cur = getattr(self, "_wg_conf_applied", None) or getattr(self, "_wg_last_conf", None)
            print(f"[{self.device_id}] [WG] ค้างหน้า LINE GAME {self._wg_line_stuck}/{lim} บน {os.path.basename(str(cur or '-'))}")
            if self._wg_line_stuck < lim or not cur or not os.path.isfile(str(cur)):
                return False
            self._wg_line_stuck = 0
            self.__dict__.setdefault("_wg_bad", set()).add(cur)
            try:
                os.remove(os.path.join(os.path.dirname(cur), ".claims", os.path.basename(cur) + ".lock"))
            except OSError:
                pass
            self._wg_conf_applied = None      # open_app ครั้งถัดไปจะต่อ IP ตัวใหม่ (VPN ไม่ปิด แค่เปลี่ยนไฟล์)
            self._wg_conf_written = None
            print(f"[{self.device_id}] [WG] LINE ไม่รับ IP {os.path.basename(cur)} - สลับ IP ใหม่ตอนเปิดเกมรอบหน้า")
            return True
        if not int(config.get("wg_enabled", 1) or 0) or getattr(self, "_wg_off", False):
            return False
        if int(config.get("wg_once", 1) or 0) and not any(k in str(reason) for k in ("fixid", "login failed")):
            return False                      # wg_once: เปลี่ยนเซิร์ฟเวอร์เฉพาะโดนบล็อก (fixid / login failed) - เน็ตแกว่ง/จอดำ/ค้าง ไม่นับ
        cur = getattr(self, "_wg_conf_applied", None) or getattr(self, "_wg_last_conf", None)
        if not cur:
            return False
        strikes = self.__dict__.setdefault("_wg_strikes", {})
        strikes[cur] = strikes.get(cur, 0) + 1
        lim = int(config.get("wg_failover_after", 2))
        print(f"[{self.device_id}] [WG] เข้าเกมไม่ได้ ({reason}) บน {os.path.basename(cur)} - ครั้งที่ {strikes[cur]}/{lim}")
        self._block_evidence(reason, cur)
        if strikes[cur] >= lim:
            self._wg_down()
            self._wg_failover(cur)
            return True
        return False

    def _net_probe(self):
        """(IP ขาออก, HTTP code ของเซิร์ฟเวอร์เกม) ผ่านเน็ตที่เกมใช้จริง
        adb shell / root ถูก Android ยกเว้นจาก VPN -> ถ้ามี tun0 ใส่ ip rule ชั่วคราวให้ root วิ่งผ่าน tunnel ก่อนยิง
        code: 401 = ต่อได้ปกติ (แค่ไม่ได้ล็อกอิน) / 000 = ไม่ตอบ (IP โดนบล็อกหรือเน็ตไม่ออก) / 403,429 = ถูกปฏิเสธ"""
        safe = self.device_id.replace(":", "_")
        sh = f"/data/local/tmp/probe_{safe}.sh"
        local = os.path.join(tempfile.gettempdir(), f"probe_{safe}.sh")
        script = chr(10).join([
            "command -v curl >/dev/null 2>&1 || { echo 'P NA NA'; rm -f " + sh + "; exit 0; }",
            "R=0",
            "if ip -o link | grep -q ' tun0'; then ip rule add pref 10500 uidrange 0-0 lookup tun0 2>/dev/null && R=1; fi",
            "IP=$(curl -s -m 8 https://api.ipify.org)",
            "C=$(curl -s -m 12 -o /dev/null -w '%{http_code}' https://rangers-api.line-apps.com/v12.3/home)",
            "[ $R = 1 ] && ip rule del pref 10500",
            "echo \"P ${IP:--} ${C:-000}\"",
            f"rm -f {sh}",
            "",
        ])
        with open(local, "w", newline=chr(10)) as f:
            f.write(script)
        self.adb_run([self.adb_cmd, "-s", self.device_id, "push", local, sh], timeout=20)
        r = self.adb_shell(f"su -c 'sh {sh}'", timeout=40)
        for line in (r.stdout or b"").decode("utf-8", "ignore").splitlines():
            if line.startswith("P "):
                parts = line.split()
                return parts[1], parts[2]
        return "-", "000"

    def _block_evidence(self, reason, conf=None):
        """เก็บหลักฐานตอนเข้าเกมไม่ได้ -> logs/block_log.csv + รูปจอ logs/block/ (ใช้หาว่าโดน block เพราะอะไร)
        api = HTTP code จากเซิร์ฟเวอร์เกมผ่านเน็ตของจอนี้: 401 = IP ใช้ได้ปกติ / 403,429,000 = IP โดน block หรือเน็ตไม่ออก"""
        if not int(config.get("block_log", 1) or 0):
            return
        try:
            ip, code = self._net_probe()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            os.makedirs(os.path.join("logs", "block"), exist_ok=True)
            shot = os.path.join("logs", "block", time.strftime("%Y%m%d_%H%M%S_") + self.device_id.replace(":", "_") + ".png")
            try:
                with open(shot, "wb") as f:
                    f.write(self.adb_run(["exec-out", "screencap", "-p"], timeout=20).stdout or b"")
            except Exception:
                shot = "-"
            new = not os.path.exists(os.path.join("logs", "block_log.csv"))
            with open(os.path.join("logs", "block_log.csv"), "a", encoding="utf-8-sig") as f:
                if new:
                    f.write("time,device,reason,server,exit_ip,api_code,screenshot" + chr(10))
                f.write(f"{ts},{self.device_id},{reason},{_wg_endpoint(conf) if conf else '-'},{ip},{code},{shot}" + chr(10))
            print(f"[{self.device_id}] [BLOCK] {reason} | IP {ip} | game api {code} | {shot}")
        except Exception as e:
            print(f"[{self.device_id}] [BLOCK] เก็บหลักฐานไม่ได้: {e}")

    def _wg_ok(self):
        """ล็อกอินผ่านบนเซิร์ฟเวอร์นี้ -> ล้างตัวนับ (นับเฉพาะครั้งที่ล้มติดกัน)"""
        cur = getattr(self, "_wg_conf_applied", None) or getattr(self, "_wg_last_conf", None)
        if cur:
            self.__dict__.setdefault("_wg_strikes", {})[cur] = 0
            # สลับ IP เป็นช่วง: ใช้เซิร์ฟเวอร์เดิมครบ wg_rotate_logins รอบ -> ปล่อยไฟล์นี้ให้จออื่น
            # แล้วเปิดเกมรอบหน้าจะต่อเซิร์ฟเวอร์ใหม่ (ไม่ตัดเน็ตตอนกำลังเล่น ; ไม่นับเป็นโดนบล็อก ไม่ติดพัก)
            n = int(config.get("wg_rotate_logins", 0) or 0)
            if n > 0:
                used = self.__dict__.setdefault("_wg_uses", {})
                used[cur] = used.get(cur, 0) + 1
                if used[cur] >= n:
                    used[cur] = 0
                    self.__dict__.setdefault("_wg_bad", set()).add(cur)
                    try:
                        os.remove(os.path.join(os.path.dirname(cur), ".claims", os.path.basename(cur) + ".lock"))
                    except OSError:
                        pass
                    print(f"[{self.device_id}] [WG] ใช้ {os.path.basename(cur)} ครบ {n} รอบ - รอบหน้าสลับ IP")

    def _wg_failover(self, conf, confirmed=False):
        """เลิกใช้ไฟล์นี้บนจอนี้ (ปล่อยการจอง) -> รอบหน้า _wg_conf_for_device จะจองไฟล์สำรองอันอื่นให้"""
        if int(config.get("wg_lock", 1) or 0):
            return None   # wg_lock: ไม่สลับเซิร์ฟเวอร์
        self.__dict__.setdefault("_wg_bad", set()).add(conf)
        _wg_cooldown_add(os.path.dirname(conf), _wg_endpoint(conf))
        # ลบไฟล์ + จดว่าโดนบล็อก 7 วัน เฉพาะตอน "ยืนยันแล้ว" (ทดสอบผ่าน VPN ได้ IP จริงแต่เซิร์ฟเวอร์เกมไม่ตอบ)
        # เข้าเกมไม่ได้เฉย ๆ อาจเป็นเน็ตกระตุก/ไอดีเสีย -> แค่พักไว้ (cooldown) ไม่ลบทิ้ง
        if confirmed and int(config.get("wg_regen_blocked", 1) or 0):
            _wg_replace_blocked(conf, self.device_id)
        try:
            os.remove(os.path.join(os.path.dirname(conf), ".claims", os.path.basename(conf) + ".lock"))
        except OSError:
            pass
        self._wg_conf_applied = None
        self._wg_conf_written = None
        print(f"[{self.device_id}] [WG] เข้าเกมไม่ได้ซ้ำบน {os.path.basename(conf)} - สลับไปเซิร์ฟเวอร์/ประเทศอื่น")

    def _wg_tuned_conf(self, conf):
        """ปรับไฟล์ก่อนส่งเข้าเครื่อง (ไม่แก้ไฟล์ต้นฉบับ):
        - IncludedApplications = เกม  -> ให้เฉพาะเกมผ่าน VPN (split tunnel) ระบบ/แอปอื่นใช้เน็ตปกติ
        - PersistentKeepalive = 25    -> ส่งสัญญาณเลี้ยง tunnel กันเราเตอร์/NAT ตัดทิ้งตอนเกมเงียบ"""
        try:
            with open(conf, encoding="utf-8-sig") as f:
                lines = f.read().replace(chr(13), "").split(chr(10))
        except Exception:
            return conf
        apps = str(config.get("wg_split_apps", "com.linecorp.LGRGS") or "").strip()   # ดีฟอลต์ = เฉพาะเกมผ่าน VPN - ถ้าทั้งเครื่องผ่าน VPN adbd ตอบกลับไม่ถึงพีซี -> adb offline/not found ทุกจอ ("" = ทั้งเครื่อง)
        ka = int(config.get("wg_keepalive", 25) or 0)
        low = [l.strip().lower() for l in lines]
        out = []
        section = ""
        for l in lines:
            s = l.strip().lower()
            if s.startswith("[") and section == "[interface]" and apps and not any(x.startswith("includedapplications") for x in low):
                out.append(f"IncludedApplications = {apps}")
            if s.startswith("["):
                section = s
            out.append(l)
        if ka and not any(x.startswith("persistentkeepalive") for x in low):
            while out and not out[-1].strip():
                out.pop()
            out.append(f"PersistentKeepalive = {ka}")
        tuned = os.path.join(tempfile.gettempdir(), f"wg_tuned_{self.device_id.replace(':', '_')}.conf")
        with open(tuned, "w", encoding="utf-8", newline=chr(10)) as f:
            f.write(chr(10).join(out).rstrip() + chr(10))
        return tuned

    def _wg_is_up(self):
        r = self.adb_shell("ip -o link show 2>/dev/null | grep -E ' tun[0-9]+:' || true", timeout=10)
        return bool((r.stdout or b"").strip())

    def _ensure_wireguard(self):
        """ต่อ WireGuard ในอีมูฯ จอนี้ (เรียกก่อนเปิดเกมทุกครั้ง - ต่ออยู่แล้วจะข้ามเร็ว)"""
        if not int(config.get("wg_enabled", 1) or 0) or getattr(self, "_wg_off", False):
            return
        conf = self._wg_conf_for_device()
        if not conf and not getattr(self, "_wg_down_done", False):
            # ไม่ได้ไฟล์รอบนี้ แต่ tunnel เก่าจากรอบก่อนยังค้าง (อาจใช้กุญแจชนกับจออื่น) -> ปิดทิ้ง
            self._wg_down_done = True
            if self._wg_is_up() and not int(config.get("wg_lock", 1) or 0):   # wg_lock: tunnel ที่เปิดอยู่ห้ามปิด
                self.adb_shell(f"su -c 'am broadcast -f 0x20 -p {self.WG_PKG} "
                               f"-a {self.WG_PKG}.action.SET_TUNNEL_DOWN --es tunnel {self.WG_TUNNEL}'", timeout=20)
                print(f"[{self.device_id}] [WG] ปิด tunnel เก่าที่ค้างอยู่ (จอนี้ไม่มีไฟล์ config ของตัวเอง)")
        if not conf:
            if not getattr(self, "_wg_warned", False):
                self._wg_warned = True
                print(f"[{self.device_id}] [WG] ไม่มีไฟล์ .conf ในโฟลเดอร์ {config.get('wg_dir', 'wg')}/ - ข้าม (ใช้เน็ตปกติ)")
            return
        if getattr(self, "_wg_conf_applied", None) == conf and self._wg_is_up():
            return
        if getattr(self, "_wg_fail", 0) >= 2:
            return                          # จอนี้ต่อ VPN ไม่ขึ้นมา 2 รอบแล้ว - เลิกลอง (ไม่เสียเวลาทุกไอดี)
        try:
            self._wg_setup(conf)
            if getattr(self, "_wg_conf_applied", None) == conf and int(config.get("wg_probe", 1) or 0):
                # ต่อแล้ว -> ยิงเซิร์ฟเวอร์เกมผ่าน VPN ทันที: ไม่ตอบ/ถูกปฏิเสธ = IP นี้โดนบล็อก -> สลับเลย ไม่ต้องรอเกมพัง
                ip, code = self._net_probe()
                # ทดสอบไม่ได้ (ไม่มี curl / ip rule ใช้ไม่ได้ -> IP เป็น "-") = ไม่ตัดสิน ปล่อยเข้าเกม
                # (เดิมเครื่องที่ทดสอบไม่ได้ถูกนับว่าโดนบล็อกทุกเซิร์ฟเวอร์ -> สลับวนไม่ได้เข้าเกมเลย)
                if code == "NA" or ip in ("-", "", "NA"):
                    print(f"[{self.device_id}] [WG] ทดสอบเซิร์ฟเวอร์เกมไม่ได้บนเครื่องนี้ (IP {ip}) - ข้ามการทดสอบ เข้าเกมเลย")
                    code = "skip"
                if code in ("000", "403", "429") and not int(config.get("wg_lock", 1) or 0):   # wg_lock: โดน block ก็ใช้ต่อ
                    print(f"[{self.device_id}] [WG] เซิร์ฟเวอร์เกมไม่รับ IP {ip} ({os.path.basename(conf)}, code {code}) - สลับเซิร์ฟเวอร์")
                    self._wg_down()
                    self._wg_failover(conf, confirmed=True)
                    tries = getattr(self, "_wg_probe_tries", 0) + 1
                    self._wg_probe_tries = tries
                    if tries < int(config.get("wg_probe_max", 5)):
                        return self._ensure_wireguard()
                    print(f"[{self.device_id}] [WG] ลองแล้ว {tries} เซิร์ฟเวอร์ยังไม่ผ่าน - เล่นต่อแบบไม่มี VPN รอบนี้")
                    self._wg_probe_tries = 0
                    return
                self._wg_probe_tries = 0
                print(f"[{self.device_id}] [WG] เซิร์ฟเวอร์เกมตอบปกติผ่าน IP {ip} (code {code})")
            if getattr(self, "_wg_conf_applied", None) == conf:
                self._wg_fail = 0
            else:
                self._wg_fail = getattr(self, "_wg_fail", 0) + 1
                if self._wg_fail >= 2:
                    print(f"[{self.device_id}] [WG] ต่อ VPN ไม่ขึ้น 2 รอบติด - ปิด VPN ของจอนี้ไปจนกว่าจะเปิดบอทใหม่ "
                          f"(ถ้าเจอ 'pm ยังไม่เจอแอป' ด้วย = MuMu จอนี้ค้าง ให้รีสตาร์ทจอ)")
        except Exception as e:
            print(f"[{self.device_id}] [WG] ต่อไม่สำเร็จ: {e} - เล่นต่อด้วยเน็ตปกติ")

    def _wg_device_is_split(self):
        """tunnel ที่เปิดค้างในจอเป็นแบบ "เฉพาะเกม" ไหม (มี IncludedApplications)
        ไฟล์ที่เขียนช่วง 4-8 ต.ค. เป็นแบบทั้งเครื่อง -> adbd ตอบกลับไม่ถึงพีซี = adb timeout/offline
        ถ้าไม่ใช่แบบเฉพาะเกม -> คืน False ให้เขียนไฟล์ใหม่ทับ (ถ้าตั้ง wg_split_apps เป็น "" = ทั้งเครื่องโดยตั้งใจ ก็ปล่อย)"""
        if not str(config.get("wg_split_apps", "com.linecorp.LGRGS") or "").strip():
            return True
        try:
            r = self.adb_shell(f"su -c 'grep -c IncludedApplications /data/data/{self.WG_PKG}/files/{self.WG_TUNNEL}.conf'", timeout=10)
            n = int(((r.stdout or b"0").decode(errors="ignore").strip().splitlines() or ["0"])[0] or 0)
        except Exception:
            n = 0
        if n <= 0:
            print(f"[{self.device_id}] [WG] tunnel ที่เปิดอยู่เป็นแบบทั้งเครื่อง (ทำ adb หลุด) - เขียนใหม่เป็นแบบเฉพาะเกม")
        return n > 0

    def _wg_must_be_up(self):
        """wg_required (default 1): เปิดเกมได้เฉพาะตอน VPN ต่ออยู่ - เน็ตบ้านเข้าเกมไม่ได้
        ล้างตัวนับที่ทำให้ _ensure_wireguard ยอมแพ้ (_wg_fail / _wg_off / probe) แล้วลองใหม่เรื่อย ๆ
        ครบ wg_required_tries ยังไม่ขึ้น -> RestartTimeoutError (clear + เริ่มใหม่) แทนการเข้าเกมด้วยเน็ตบ้าน"""
        if not int(config.get("wg_enabled", 1) or 0):
            return
        if not int(config.get("wg_required", 0) or 0):   # 0 = VPN ไม่ขึ้นก็เล่นต่อด้วยเน็ตบ้าน (แบบเดิม)
            return self._ensure_wireguard()
        if (int(config.get("wg_once", 1) or 0) and not getattr(self, "_wg_conf_applied", None)
                and not getattr(self, "_wg_started", False) and self._wg_is_up()
                and self._wg_device_is_split()):
            # ตอนเริ่ม: tunnel เปิดแช่อยู่แล้ว -> ใช้ต่อเลย ห้ามยุ่ง (ไม่เขียนไฟล์ใหม่/ไม่ปิดเปิด)
            self._wg_started = True
            self._wg_conf_applied = self._wg_conf_for_device() or "(เปิดอยู่แล้ว)"
            self._wg_last_conf = self._wg_conf_applied if os.path.isfile(str(self._wg_conf_applied)) else None
            print(f"[{self.device_id}] [WG] VPN เปิดอยู่แล้ว - ใช้ต่อ ไม่ยุ่ง")
            return
        if (int(config.get("wg_once", 1) or 0) and getattr(self, "_wg_started", False)
                and getattr(self, "_wg_conf_applied", None) and self._wg_is_up()):
            return                          # เปิดแช่อยู่ -> ห้ามยุ่ง (แม้ไฟล์ใน wg/ ถูกสร้างใหม่ตามรอบแล้วก็ตาม)
        self._wg_started = True
        tries = int(config.get("wg_required_tries", 10) or 10)
        for n in range(1, tries + 1):
            self._wg_off = False
            self._wg_fail = 0
            self._wg_probe_tries = 0
            self.last_activity_time = time.time()
            self._ensure_wireguard()
            if getattr(self, "_wg_conf_applied", None) and self._wg_is_up():
                return
            _c = self._wg_conf_for_device()
            _why = ("ไม่มีไฟล์ .conf ใน " + str(config.get("wg_dir", "wg")) + "/") if not _c else ("ใช้ " + os.path.basename(_c) + " แต่ tunnel ไม่ขึ้น")
            print(f"[{self.device_id}] [WG] VPN ยังไม่ขึ้น ({n}/{tries}) - {_why} - ลองใหม่...")
            self._wg_bad = set()            # ให้วนกลับไปลองทุกเซิร์ฟเวอร์ได้อีก
            _real_sleep(5)
        print(f"[{self.device_id}] [WG] ต่อ VPN ไม่ขึ้น {tries} รอบ - รีสตาร์ทไฟล์นี้แทนการเข้าเกมด้วยเน็ตบ้าน")
        raise GameCrashed("wg_required: VPN not up")

    def _wg_setup(self, conf):
        pkg, name = self.WG_PKG, self.WG_TUNNEL
        # ทางลัด: ไฟล์ config นี้เคยเขียนลงเครื่องแล้ว (แค่ปิดไปตอนส่งไฟล์) -> สั่งเปิด tunnel อย่างเดียว ไม่ต้องส่งไฟล์ใหม่
        # (เทียบเวลาแก้ไฟล์ด้วย - server ส่งไฟล์ชื่อเดิมแต่เนื้อหาใหม่มา ต้องเขียนลงเครื่องใหม่)
        if getattr(self, "_wg_conf_written", None) == (conf, os.path.getmtime(conf)):
            self.adb_shell(f"su -c 'am broadcast -f 0x20 -p {pkg} -a {pkg}.action.SET_TUNNEL_UP --es tunnel {name}'",
                           timeout=20)
            for _ in range(8):
                _real_sleep(0.5)
                if self._wg_is_up():
                    self._wg_conf_applied = conf
                    print(f"[{self.device_id}] [WG] ต่อ VPN แล้ว ({os.path.basename(conf)})")
                    return
            self._wg_conf_written = None    # ไม่ขึ้น -> รอบนี้ทำแบบเต็มต่อเลย
        # 1) ลงแอป WireGuard ถ้ายังไม่มี - เช็คจากโฟลเดอร์แอปในเครื่อง (ไม่พึ่ง pm ที่ตอบช้าตอนเครื่องโหลดหนัก)
        #    เคยเจอ: pm ตอบไม่ทัน -> นึกว่ายังไม่ลง -> ติดตั้ง 17 MB ซ้ำทุกจอทุกรอบ -> pm ยิ่งช้า หาแม้แต่ตัวเกมไม่เจอ
        if not getattr(self, "_wg_installed", False):
            r = self.adb_shell(f"su -c 'test -d /data/data/{pkg} && echo YES'", timeout=15)
            if b"YES" in (r.stdout or b""):
                self._wg_installed = True
            elif getattr(self, "_wg_install_tried", False):
                print(f"[{self.device_id}] [WG] ติดตั้ง WireGuard ไปแล้วรอบนี้แต่ยังไม่เห็นแอป - ไม่ติดตั้งซ้ำ (ข้าม VPN ของจอนี้ไปก่อน)")
                return
            else:
                apk = _wg_apk_path()
                if not apk:
                    print(f"[{self.device_id}] [WG] ไม่มีไฟล์แอป WireGuard (โหลดไม่ได้) - ข้าม")
                    return
                self._wg_install_tried = True
                print(f"[{self.device_id}] [WG] ติดตั้งแอป WireGuard (ครั้งเดียว)...")
                self.adb_run([self.adb_cmd, "-s", self.device_id, "install", "-r", apk], timeout=180)
                r = self.adb_shell(f"su -c 'test -d /data/data/{pkg} && echo YES'", timeout=15)
                self._wg_installed = b"YES" in (r.stdout or b"")

        # 2) เขียนไฟล์ tunnel + ตั้งค่าแอป (ต้องปิดแอปก่อน ไม่งั้นมันเขียนทับ)
        self.adb_shell(f"am force-stop {pkg}")
        safe_dev = self.device_id.replace(":", "_")
        tmp_conf = f"/data/local/tmp/wg_{safe_dev}.conf"
        tmp_pb = f"/data/local/tmp/wg_{safe_dev}.pb"
        local_pb = os.path.join(tempfile.gettempdir(), f"wg_{safe_dev}.pb")
        # DataStore (protobuf) ของแอป: allow_remote_control_intents = true
        key = b"allow_remote_control_intents"
        entry = b"\x0a" + bytes([len(key)]) + key + b"\x12\x02\x08\x01"
        with open(local_pb, "wb") as f:
            f.write(b"\x0a" + bytes([len(entry)]) + entry)
        # คำสั่ง root เขียนเป็นสคริปต์แล้วรันทีเดียว - su ของ MuMu กิน $ / quote ถ้าส่งเป็นสตริงตรง ๆ
        d = f"/data/data/{pkg}"
        tmp_sh = f"/data/local/tmp/wg_{safe_dev}.sh"
        local_sh = os.path.join(tempfile.gettempdir(), f"wg_{safe_dev}.sh")
        script = chr(10).join([
            f"mkdir -p {d}/files/datastore",
            f"cp {tmp_conf} {d}/files/{name}.conf",
            f"cp {tmp_pb} {d}/files/datastore/settings.preferences_pb",
            f"U=$(stat -c %u {d})",
            f"chown -R $U:$U {d}/files",
            f"chmod -R 700 {d}/files",
            f"restorecon -R {d}/files 2>/dev/null",
            f"rm -f {tmp_conf} {tmp_pb}",
            # อนุญาตสร้าง VPN ล่วงหน้า (ไม่ต้องกดยืนยันบนจอ)
            f"cmd appops set {pkg} ACTIVATE_VPN allow",
            # Android 12 ห้ามแอปเปิด VpnService จากเบื้องหลัง -> ใส่ไว้ในรายการยกเว้นประหยัดแบต (ทดสอบบน MuMu แล้วผ่าน)
            f"cmd deviceidle whitelist +{pkg}",
            # สั่งเปิด tunnel เป็น root (ผ่านสิทธิ์ CONTROL_TUNNELS) ; -f 0x20 = ส่งถึงแอปที่ถูก force-stop
            f"am broadcast -f 0x20 -p {pkg} -a {pkg}.action.SET_TUNNEL_UP --es tunnel {name}",
            f"rm -f {tmp_sh}",
            "",
        ])
        with open(local_sh, "w", newline=chr(10)) as f:   # ไฟล์ sh ต้องเป็น LF
            f.write(script)
        self._wg_last_conf = conf
        for src, dst in ((self._wg_tuned_conf(conf), tmp_conf), (local_pb, tmp_pb), (local_sh, tmp_sh)):
            self.adb_run([self.adb_cmd, "-s", self.device_id, "push", src, dst], timeout=30)
        self.adb_shell(f"su -c 'sh {tmp_sh}'", timeout=30)   # ต้องครอบ quote: su ของ MuMu รับแค่คำถัดไปคำเดียว
        for _ in range(10):
            _real_sleep(1)
            if self._wg_is_up():
                self._wg_conf_applied = conf
                self._wg_conf_written = (conf, os.path.getmtime(conf))
                print(f"[{self.device_id}] [WG] ต่อ VPN แล้ว ({os.path.basename(conf)})")
                return
        print(f"[{self.device_id}] [WG] สั่งต่อแล้วแต่ยังไม่เห็น tunnel ภายใน 10 วิ ({os.path.basename(conf)})")

    def _clear_proxy_for_device(self):
        """Best-effort clean-up for proxy settings when disabled or restarting."""
        try:
            # ":0" = ล้าง proxy แบบที่ Android ยอมรับแน่นอน (ใส่ "" บางเวอร์ชันไม่ถือว่าล้าง) แล้วลบคีย์ทิ้ง
            self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "settings", "put", "global", "http_proxy", ":0"], timeout=12)
            for key in ("http_proxy", "global_http_proxy_host", "global_http_proxy_port", "proxy_exclusion_list"):
                self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "settings", "delete", "global", key], timeout=12)
            return True
        except Exception:
            return False

    def _resolve_game_activity(self):
        """หา launcher activity จริงของเกมจากเครื่อง (ชื่อเปลี่ยนตามเวอร์ชันเกม) แล้ว cache ไว้
        - เดิม hardcode com.linecorp.common.activity.LineActivity ซึ่งเกมเวอร์ชันใหม่เปลี่ยนเป็น .LineRangersAdr แล้ว
        - ถามจากเครื่องตรงๆ จะได้ไม่พังอีกเวลาเกมเปลี่ยนชื่อ activity"""
        cached = getattr(self, "_game_activity", None)
        if cached:
            return cached
        try:
            r = self.adb_run([
                self.adb_cmd, "-s", self.device_id, "shell",
                "cmd", "package", "resolve-activity", "--brief", "com.linecorp.LGRGS"
            ], timeout=8)
            out = r.stdout if isinstance(r.stdout, str) else (r.stdout or b"").decode("utf-8", "ignore")
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("com.linecorp.LGRGS/"):
                    self._game_activity = line
                    print(f"[{self.device_id}] [APP] Launcher activity: {line}")
                    return line
        except Exception:
            pass
        # fallback: ชื่อ activity ของเกมเวอร์ชันปัจจุบัน
        self._game_activity = "com.linecorp.LGRGS/.LineRangersAdr"
        return self._game_activity

    def adb_shell(self, shell_cmd, timeout=10):
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(
            [self.adb_cmd, "-s", self.device_id, "shell", shell_cmd],
            capture_output=True, timeout=timeout, **kwargs)

    def open_app(self):
        self.last_activity_time = time.time()
        # ต่อ VPN แยกของจอนี้ก่อนเปิดเกม (เหมือน LGR/login.py)
        self._clear_proxy_for_device()
        if int(config.get("wg_after_login", 0) or 0):
            self._wg_down()
        else:
            self._wg_must_be_up()
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
                self._wg_strike("login failed")
                self._login_fixid_count = 0
                self.handle_login_failure()
                return "failed"

            # === fixid.png Check (เช็คทุกรอบ) -> fikcheck -> refresh -> check ===
            if self.exists_in_cache("img/fixid.png", threshold=0.95):
                self._login_fixid_count += 1
                self.log(f"Found fixid.png ({self._login_fixid_count}/3), fikcheck -> refresh -> check...")
                
                if self._login_fixid_count >= 3:
                    self.log(f"fixid limit reached (3 times)! Failing...")
                    self._wg_auth_failed()
                    self._wg_strike("fixid ครบ")
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
            self.handle_event_back()
            
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
        # Stall watchdog: nothing tapped for STALL_TIMEOUT_SEC -> hand over to RE-LOOP.
        # Skipped while a recovery is already running, otherwise it would re-trigger itself.
        if not self._in_reloop and time.time() - self.last_activity_time > STALL_TIMEOUT_SEC:
            mins = int((time.time() - self.last_activity_time) / 60)
            self.log(f"!!! STALL: no progress for {mins} min (limit {STALL_TIMEOUT_SEC // 60}) !!!")
            raise StallDetected()

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
                # Network popups can hit at any time, including mid-stage long after
                # login, so this one is never gated on login_done.
                self.check_network_popups()

                if not getattr(self, "login_done", False):
                    self.check_floating_popups()
                
                # Level verify is NOT done here anymore.
                # It runs exactly once right after login: see verify_account_level_once().
            except (AccountFinished, GameCrashed):
                raise # Must propagate to run_step1 so the account is switched, not retried
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
        self.last_activity_time = time.time()
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

    def press_esc(self):
        self.log(f"Pressing ESC (ADB shell KEYCODE_ESCAPE)")
        subprocess.run(
            [self.adb_cmd, "-s", self.device_id, "shell", "input", "keyevent", "KEYCODE_ESCAPE"],
            **self.kwargs
        )

    def swipe(self, x1, y1, x2, y2, duration=1000):
        self.log(f"Dragging from ({x1}, {y1}) to ({x2}, {y2}) over {duration}ms...")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)], **self.kwargs)

    def find_image(self, template_path, threshold=0.8):
        if self.screen_bgr is None: return None
        template = load_template(template_path)
        if template is None: return None
        res = cv2.matchTemplate(self.screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= threshold)
        if len(loc[0]) > 0:
            h, w, _ = template.shape
            return (int(loc[1][0] + w/2), int(loc[0][0] + h/2))
        return None

    def find_image_in_region(self, template_path, region, threshold=0.8):
        if self.screen_bgr is None: return None
        rx, ry, rw, rh = region
        region_bgr = self.screen_bgr[ry:ry+rh, rx:rx+rw]
        template = load_template(template_path)
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
        template = load_template(template_path)
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

    def check_network_popups(self):
        """The fixnet family ("Unstable network connection / retry"). Checked before every
        other popup and on every capture, logged in or not: a network popup sits on top of
        everything, so nothing else can be found or clicked until it is gone.
        Returns True if one was handled."""
        found_any = False

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

        return found_any

    def check_floating_popups(self):
        """
        Check and click floating popups (fixnet family first, then checkline / fixplay / ...).
        Loops until no popups are found.
        """
        max_popups_iterations = 5
        for _ in range(max_popups_iterations):
            # Network popups always go first - they cover every other button on screen
            found_any = self.check_network_popups()
            if found_any:
                self._raw_capture()

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

            # fixplay.png: Google Play Popup
            if self.exists_in_cache("img/fixplay.png"):
                self.log(f"[POPUP] fixplay.png detected! Clicking OK...")
                if self.exists_in_cache("img/cancel.png"): 
                    self.click("img/cancel.png"); time.sleep(1); self._raw_capture()
                self.click("img/ok.png")
                time.sleep(2)
                found_any = True

            if not found_any:
                break
            else:
                self._raw_capture() # Update cache for next iteration

    def handle_event_back(self, max_back=10):
        """event.png -> tap it, then press BACK over and over until cancel.png shows up
        (click it, and clear a stacked event behind it) or stoplogin.png appears.
        Expects a fresh capture in the cache. Returns True if an event was handled."""
        if not self.exists_in_cache("img/event.png", threshold=0.95):
            return False

        pos_ev = self.find_image("img/event.png", threshold=0.95)
        self.log(f"Found event.png, handling (Delay 2s)...")
        self.tap(pos_ev[0], pos_ev[1])
        time.sleep(2)

        back_count = 0
        while back_count < max_back:
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

        return True

    def _spam_until(self, stop_img, action="esc", label="spam", timeout=120, threshold=0.8):
        """Repeat `action` until stop_img shows up, then dismiss it and stop.
        action is either "esc" (ESC key) or an image path to tap."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.capture_screen()
            if self.find_image(stop_img, threshold=threshold):
                self.log(f"{label}: {os.path.basename(stop_img)} appeared -> stopping.")
                self.click(stop_img, threshold=threshold)
                time.sleep(1)
                return True
            if action == "esc":
                self.press_esc()
            else:
                self.click(action, threshold=threshold)
            time.sleep(0.5)
        self.log(f"{label}: gave up after {timeout}s without seeing {os.path.basename(stop_img)}.")
        return False

    def handle_reloop(self, wait_timeout=120):
        """RE-LOOP recovery after a stall: clear the app, reopen the game, then take
        whichever of the five known recovery screens is showing and get back on track.

        Returns "gacha" or "egear" when the caller must restart that sub-sequence,
        otherwise "resume" (back on the map -> carry on with the current step).
        """
        if self._in_reloop:
            return "resume"
        self._in_reloop = True
        try:
            self.log(f"=== RE-LOOP: clearing app and reopening the game ===")
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"], **self.kwargs)
            time.sleep(3)
            self.open_app()
            time.sleep(5)

            # Same event -> BACK spam as the login flow: an event popup right after the
            # game starts would otherwise hide every recovery screen below.
            self.capture_screen()
            if self.handle_event_back():
                self.log(f"RE-LOOP: cleared an event popup after start.")

            branches = [
                ("check1",    "img/heyquest1/check1-reloop.bmp"),
                ("check2",    "img/heyquest2/check2-reloop.bmp"),
                ("heygacha",  "img/heygacha/heygacha.bmp"),
                ("heygacha2", "img/heygacha2/heygacha2.bmp"),
                ("heybrown",  "img/heybrown/heybrown.bmp"),
            ]

            found = None
            deadline = time.time() + wait_timeout
            while time.time() < deadline:
                self.capture_screen()
                # keep clearing event popups while we hunt for a recovery screen
                if self.handle_event_back():
                    self.capture_screen()
                for name, path in branches:
                    if self.find_image(path, threshold=0.8):
                        found = name
                        break
                if found:
                    break
                rem = int(deadline - time.time())
                self.log(f"RE-LOOP: waiting for a recovery screen ({rem}s left)...")
                time.sleep(2)

            if found is None:
                self.log(f"RE-LOOP: none of the 5 screens showed up. Falling back to mainstage.")
                self.wait_and_click("img/mainstage.png", timeout=30)
                return "resume"

            self.log(f"RE-LOOP: matched {found}.")

            # check1-reloop: sp-stage -> ESC until cancel -> skip -> skipok -> mainstage
            if found == "check1":
                self.wait_and_click("img/heyquest1/sp-stage.bmp", timeout=15)
                self._spam_until("img/cancel.png", action="esc", label="RE-LOOP check1 ESC")
                self.wait_and_click("img/skip.png", timeout=15)
                self.wait_and_click("img/skipok.png", timeout=15)
                self.wait_and_click("img/mainstage.png", timeout=30)
                return "resume"

            # check2-reloop: skipnew -> skipok -> sp-stage until cancel -> mainstage
            if found == "check2":
                self.wait_and_click("img/heyquest2/skipnew.bmp", timeout=15)
                self.wait_and_click("img/skipok.png", timeout=15)
                self._spam_until("img/cancel.png", action="img/heyquest2/sp-stage.bmp",
                                 label="RE-LOOP check2 sp-stage")
                self.wait_and_click("img/mainstage.png", timeout=30)
                return "resume"

            # heygacha / heygacha2 / heybrown: same cutscene skip, different restart point
            self.wait_and_click("img/skip.png", timeout=15)
            self.wait_and_click("img/skipok.png", timeout=15)
            if found == "heybrown":
                self.log(f"RE-LOOP: restarting from egear1.")
                return "egear"
            self.log(f"RE-LOOP: restarting from gacha.")
            return "gacha"
        finally:
            self._in_reloop = False
            self.last_activity_time = time.time()

    def verify_account_level_once(self, timeout=30):
        """Run the account level gate EXACTLY ONCE, right after login (stoplogin found).
        Waits for the level badge, reads it, then decides:
          level <= 4  -> play normally (stage 1 .. 150)
          level  > 4  -> move account to lv5+ and switch (raises AccountFinished)
        Never checked again afterwards, because the level rises while playing.
        """
        if self.config.get("skip-lv", 0) != 1:
            return

        self._checklv_done = False
        self.log(f"[CHECK-LV] Verifying account level once after login (timeout {timeout}s)...")

        start = time.time()
        while time.time() - start < timeout:
            self.capture_screen()
            self.check_account_level()  # raises AccountFinished if level > 4
            if self._checklv_done:
                break
            time.sleep(1)

        if not self._checklv_done:
            self.log(f"[CHECK-LV] Level not confirmed within {timeout}s. Continuing normally.")

        # Lock it down: no level check for the rest of this account's run
        self._checklv_done = True

    def check_account_level(self):
        """Dedicated level verification with enhanced preprocessing for high accuracy"""
        skip_lv = self.config.get("skip-lv", 0)
        if skip_lv != 1 or getattr(self, "_checklv_done", False):
            return

        if self.exists_in_cache("img/checkpont-lv.png"):
            self.log(f"[CHECK-LV] Target found! Verifying level...")
            self._checklv_done = True
            
            # 1. Capture & Crop Region
            # Region: 25, 17, 81, 74 -- plus a padded version, in case the digits
            # sit a few pixels outside it on some resolutions.
            region = (25, 17, 81, 74)
            rx, ry, rw, rh = region
            src_h, src_w = self.screen_bgr.shape[:2]
            crops = []
            for pad in (0, 12):
                x0 = max(0, rx - pad); y0 = max(0, ry - pad)
                x1 = min(src_w, rx + rw + pad); y1 = min(src_h, ry + rh + pad)
                c = self.screen_bgr[y0:y1, x0:x1]
                if c.size > 0:
                    crops.append((pad, c))

            # 2. Preprocessing variants. A single OTSU-inverted pass only works when the
            # badge is light-on-dark; light backgrounds came out inverted and unreadable,
            # which is what made this log "Could not read level reliably" over and over.
            def _prep_variants(img_crop):
                gray = cv2.cvtColor(img_crop, cv2.COLOR_BGR2GRAY)
                for scale in (3.0, 5.0):
                    resized = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                    blurred = cv2.GaussianBlur(resized, (3, 3), 0)
                    _, t_inv = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                    yield (f"otsu-inv@{scale:g}x", t_inv)
                    _, t_bin = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                    yield (f"otsu@{scale:g}x", t_bin)
                    yield (f"adaptive@{scale:g}x", cv2.adaptiveThreshold(
                        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 21, 6))
                    yield (f"gray@{scale:g}x", resized)

            import easyocr
            import re
            if self._ocr_reader is None:
                self.log(f"[OCR] Initializing for Level Check...")
                self._ocr_reader = easyocr.Reader(['en'], gpu=False)

            # 3. Run the variants until one reads a plausible level confidently.
            # The old single-variant path is tried first, so a clean badge costs
            # exactly as much OCR as before; only failures pay for the extra passes.
            found_lv = None
            max_conf = 0.0
            best_tag = ""
            for pad, crop in crops:
                for tag, prepped in _prep_variants(crop):
                    try:
                        results = self._ocr_reader.readtext(prepped, allowlist='0123456789')
                    except Exception as e:
                        self.log(f"[CHECK-LV] OCR error on {tag}: {e}")
                        continue
                    for (bbox, text, conf) in results:
                        digits = re.findall(r'\d+', text)
                        if not digits:
                            continue
                        val = int(digits[0])
                        # Sanity: a checkpoint level is 1..99
                        if not (1 <= val <= 99):
                            continue
                        if conf > max_conf:
                            found_lv, max_conf, best_tag = val, conf, f"pad{pad}/{tag}"
                    if max_conf >= 0.50:
                        break
                if max_conf >= 0.50:
                    break

            # Accept anything the allowlist read with some confidence. The old floor of
            # 0.30 threw away readings that were right but slightly fuzzy.
            if max_conf < 0.15:
                found_lv = None
            elif best_tag:
                self.log(f"[CHECK-LV] Read via {best_tag}")
            
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
        self.root_shell(f"chmod 666 {remote_path}")
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

    def find_image_any(self, img_paths, threshold=0.8):
        """Return (pos, matched_path) for the FIRST image found out of img_paths.
        Used where the game has two art variants of the same button
        (e.g. update1.png / update1new.png, drag1.png / drag1new.png)."""
        for img in img_paths:
            pos = self.find_image(img, threshold)
            if pos:
                return pos, img
        return None, None

    def run_update_flow(self, label="Update", timeout=60, finish_always=True):
        """Hero level-up (update) flow:
            update1 | update1new  -> tap
            update2              -> tap
            drag1  | drag1new    -> swipe hero (99,447) -> (395,259)
            update3              -> tap x6 (2s apart)
            skip -> skipok -> updateback -> mainstage
        Returns True if the drag step was reached (flow actually ran), else False.
        finish_always=False skips the closing skip/skipok/updateback/mainstage
        when nothing showed up, so we do not waste time when there is no update screen."""
        UPDATE1 = ["img/update1.png", "img/update1new.png"]
        DRAG1   = ["img/drag1.png", "img/drag1new.png"]

        upd_start = time.time(); upd_last_log = time.time(); found_drag = False
        self.log(f"{label}: Looking for update1/update1new/update2/drag1/drag1new (timeout {timeout}s)...")

        while time.time() - upd_start < timeout:
            self.capture_screen()

            pos_u1, hit_u1 = self.find_image_any(UPDATE1, 0.8)
            if pos_u1:
                self.tap(pos_u1[0], pos_u1[1], label=os.path.basename(hit_u1))
                time.sleep(1); continue

            u2 = self.find_image("img/update2.png", 0.8)
            if u2:
                self.tap(u2[0], u2[1], label="update2")
                time.sleep(1); continue

            pos_drag, hit_drag = self.find_image_any(DRAG1, 0.85)
            if pos_drag:
                self.log(f"Found {os.path.basename(hit_drag)} at {pos_drag}, Sweeping from 99, 447 to 395, 259 (800ms)...")
                subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", "99", "447", "395", "259", "800"], **self.kwargs)
                time.sleep(1); found_drag = True; break

            if time.time() - upd_last_log > 5:
                rem = int(timeout - (time.time() - upd_start))
                self.log(f"...searching for update1/update1new/update2/drag1/drag1new ({rem}s remaining of {timeout}s)")
                upd_last_log = time.time()
            time.sleep(1)

        if not found_drag:
            self.log(f"{label}: update/drag1 not found within {timeout}s, skipping update flow...")
            if not finish_always:
                return False
        else:
            # Click update3 and repeat slowly (6 taps with 2s delay)
            p_u3 = self.wait_and_click("img/update3.png", timeout=20)
            if p_u3:
                self.log(f"Clicking update3 repeats (Slower: 2s delay)...")
                for _ in range(5):
                    time.sleep(2.0)
                    self.tap(p_u3[0], p_u3[1], label="update3-repeat")

        # Finishing Flow (ensure we end up back on the map)
        self.log(f"{label}: Executing finishing sequence...")
        self.wait_and_click("img/skip.png", timeout=15)
        self.wait_and_click("img/skipok.png", timeout=15)
        self.wait_and_click("img/updateback.png", timeout=15)
        self.wait_and_click("img/mainstage.png", timeout=15)
        return found_drag

    def run_7day_flow(self, label="7Day", timeout_first=15, timeout_rest=10):
        """7-day reward flow, in order:
            7day1 -> skip (10s) -> skipok (10s) -> 7day2 -> 7day3 -> 7day4
        7day1 is the entry: if it never shows up within timeout_first there is no 7-day
        popup on screen, so the rest is skipped instead of burning a timeout on each step."""
        self.log(f"{label}: Looking for 7day1.png (timeout {timeout_first}s)...")
        if not self.wait_and_click("img/7day1.png", timeout=timeout_first):
            self.log(f"{label}: 7day1.png not found within {timeout_first}s, skipping 7day flow.")
            return False

        time.sleep(1)
        self.log(f"{label}: 7day found. Clearing skip / skipok (10s each), then 7day2 -> 7day4...")
        self.wait_and_click("img/skip.png", timeout=10)
        self.wait_and_click("img/skipok.png", timeout=10)

        for i in (2, 3, 4):
            img = f"img/7day{i}.png"
            if self.wait_and_click(img, timeout=timeout_rest):
                time.sleep(1)
            else:
                self.log(f"{label}: 7day{i}.png not found within {timeout_rest}s, continuing...")
        self.log(f"{label}: 7day sequence finished.")
        return True

    def run_stage6_detour(self, label="Stage 6 detour"):
        """Stage 6 is not showing on the map. Clear what usually covers it, in this order:
            quest (15s, then tapped repeatedly until quest1 appears) -> quest1 -> quest2 -> quest3
            -> mainstage -> ESC once -> skip -> skipok
            -> 7day1 (15s) -> rest of the 7day flow
        Then the caller goes back to hunting num6."""
        self.log(f"{label}: Stage 6 not found -> checking quest / 7day popups...")

        pos_q = self.wait_and_click("img/quest.png", timeout=15)
        if pos_q:
            # Keep tapping quest.png. quest1.png showing up is the ONLY stop signal.
            q_timeout = 60
            q_start = time.time(); q_last_log = time.time(); q_taps = 1; saw_q1 = False
            self.log(f"{label}: quest found. Tapping quest until quest1.png appears (timeout {q_timeout}s)...")

            while time.time() - q_start < q_timeout:
                self.capture_screen()

                pos_q1 = self.find_image("img/quest1.png", 0.8)
                if pos_q1:
                    self.log(f"{label}: quest1.png appeared after {q_taps} quest tap(s). Stop tapping quest.")
                    self.tap(pos_q1[0], pos_q1[1], label="quest1")
                    saw_q1 = True
                    time.sleep(1)
                    break

                pos_qq = self.find_image("img/quest.png", 0.8)
                if pos_qq:
                    pos_q = pos_qq
                    self.tap(pos_qq[0], pos_qq[1], label="quest")
                else:
                    self.tap(pos_q[0], pos_q[1], label="quest-repeat")
                q_taps += 1

                if time.time() - q_last_log > 5:
                    rem = int(q_timeout - (time.time() - q_start))
                    self.log(f"...tapping quest, waiting for quest1.png ({rem}s remaining of {q_timeout}s)")
                    q_last_log = time.time()
                time.sleep(1)

            if not saw_q1:
                self.log(f"{label}: quest1.png never appeared within {q_timeout}s ({q_taps} taps). Continuing anyway...")

            for q in (2, 3):
                self.wait_and_click(f"img/quest{q}.png", timeout=15)
                time.sleep(1)
        else:
            self.log(f"{label}: quest.png not found within 15s.")

        self.log(f"{label}: Going to map (mainstage), then pressing ESC once...")
        self.wait_and_click("img/mainstage.png", timeout=15)
        time.sleep(2)
        self.press_esc()
        time.sleep(2)

        self.log(f"{label}: Clearing skip / skipok before 7day...")
        self.wait_and_click("img/skip.png", timeout=15)
        self.wait_and_click("img/skipok.png", timeout=15)

        self.run_7day_flow(label, timeout_first=15, timeout_rest=10)
        return True

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
            
            def _stage12_gacha_seq():
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
            
                # skip -> skipok before hunting gearep2 (a cutscene can sit in front of it)
                wc("img/skip.png", 15)
                wc("img/skipok.png", 15)

                # gearep2 -> skip -> skipok
                wc("img/gearep2.png", 300)
                wc("img/skip.png", 15)
                wc("img/skipok.png", 15)
            
                # fixgear4-1 -> gearep3 -> repeat tap position 5 times
                pos_fg = wc("img/fixgear4-1.png", 30)
                if pos_fg:
                    self.log(f"Repeating tap on fixgear4-1 x4...")
                    for _ in range(4):
                        self.tap(pos_fg[0], pos_fg[1], label="fixgear4-1-repeat")
                        time.sleep(0.5)
            
                pos_g3 = wc("img/gearep3.png", 50)
                if pos_g3:
                    self.log(f"Repeating tap on gearep3 x9...")
                    for _ in range(9):
                        self.tap(pos_g3[0], pos_g3[1], label="gearep3-repeat")
                        time.sleep(0.5)
            
                # gearep4 -> skip -> skip 10s -> backgearep1 -> mainstage
                wc("img/gearep4.png", 300)
                wc("img/skip.png", 30)
                wc("img/skipok.png",30)
                time.sleep(10)
            
                self.log(f"Stage 12: Tapping backgearep1.png until gone...")
                while wc("img/backgearep1.png", 5):
                    time.sleep(0.5)
                wc("img/mainstage.png", 80)
            
                self.log(f"=== STAGE 12 SEQUENCE FINISHED ===")
                return True

            # A stall in here means RE-LOOP sends us back to the start of the gacha run
            while True:
                try:
                    _stage12_gacha_seq()
                    break
                except StallDetected:
                    self.log(f"Stage 12: stall during gacha -> RE-LOOP, then restarting from gacha1...")
                    self.handle_reloop()
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
            def _stage15_egear_seq():
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

            # A stall in here means RE-LOOP sends us back to the start of the egear run
            while True:
                try:
                    _stage15_egear_seq()
                    break
                except StallDetected:
                    self.log(f"Stage 15: stall during egear -> RE-LOOP, then restarting from egear1...")
                    self.handle_reloop()
            return True

        # ============================================================
        # INITIAL REWARD COLLECTION (For all other NORMAL stages)
        # ============================================================
        if stage_num == 27:
            self.log(f"Stage 27 special: Waiting 15s before starting clear sweep...")
            time.sleep(15)
            
        reward_sweep("Initial Sweep")

        if stage_num == 11:
            self.log(f"Stage 11: Starting update and finishing flow...")
            wc("img/skip.png", 15); wc("img/skipok.png", 15)

            self.run_update_flow("Stage 11", timeout=60, finish_always=True)

            reward_sweep("Stage 11 Cleanup", timeout_idle=5)
            return True

        # ============================================================
        # DEFAULT WRAP-UP
        # ============================================================
        # Original sequential wrap-up: one pass, no retry loop. Slow but it never hangs.
        # The only change is the map check up front -- if the next stage is already
        # reachable there is nothing to skip, so don't sit through the waits.
        self.capture_screen()
        if self.find_image("img/waitmainstage.png", 0.8):
            self.log(f"Wrap-up: already on the map, moving to the next stage.")
        else:
            wc("img/skip.png", 15)
            wc("img/skipok.png", 15)
            if wc("img/event.png", 8):
                self.log(f"Found event.png during wrap-up, cleared it.")
            wc("img/mainstage.png", 15)

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
                # One spawn per 10ms floods Windows with ~47 processes/sec per device.
                # The chain already carries a full burst of taps, so pacing it costs
                # almost no taps but far less CPU.
                time.sleep(TAP_BURST_INTERVAL)

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
                    self._wg_ok()
                    if int(config.get("wg_after_login", 0) or 0):
                        self._ensure_wireguard()
                    self.log(f"Login complete for {fname}. Routine starting...")

                    # One-time account level gate (runs once here, never during play)
                    self.verify_account_level_once(timeout=30)
            
                    # --- MAIN PLAY SEQUENCE ---
                    if self.config.get("maketeam", 1) == 1:
                        self.find_team()
                    else:
                        self.log(f"maketeam=0 -> Skipping find_team, going straight to stages.")

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

                    stage_idx = 0
                    while stage_idx < len(stage_sequence):
                        stage_info = stage_sequence[stage_idx]
                        try:
                            target_img = stage_info["img"]
                            region     = stage_info["region"]
                            stage_num  = stage_info["num"]

                            # Stage 5 can leave a cutscene + the 7-day popup in front of the map
                            if stage_num == 6:
                                self.log(f"Before Stage 6: Clearing skip / skipok (2 rounds, 15s each), then 7day...")
                                for r in (1, 2):
                                    self.log(f"Before Stage 6: skip / skipok round {r}/2...")
                                    self.wait_and_click("img/skip.png", timeout=15)
                                    self.wait_and_click("img/skipok.png", timeout=15)
                                # Entry is 7day1.png (plain 7day.png is never tapped)
                                self.run_7day_flow("Before Stage 6", timeout_first=15, timeout_rest=10)

                            # Level-up (update) screen can appear after Stage 6 -> handle it before hunting Stage 7
                            if stage_num == 7:
                                self.run_update_flow("Before Stage 7", timeout=60, finish_always=False)

                            # fixback.png can show up before Stage 23 -> tap it if it appears
                            if stage_num == 23:
                                self.log(f"Before Stage 23: searching for fixback.png (10s)...")
                                if self.wait_and_click("img/fixback.png", timeout=10):
                                    self.log(f"Clicked fixback.png before Stage 23.")
                                    time.sleep(2)

                            self.log(f"=== TARGET STAGE: {stage_num} (IMAGE) ===")

                            retry_find_stage = 0
                            stage_notfound_count = 0
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
                                        template_chest = load_template("img/chest1.png")
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

                                    stage_notfound_count += 1
                                    self.log(f"Stage {stage_num} image not found. (Retry: {stage_notfound_count}/5)")

                                    # Stage 6 hidden 5 times in a row -> quest / 7day popup is usually in the way
                                    if stage_num == 6 and stage_notfound_count >= 5:
                                        self.log(f"Stage 6 not found 5 times -> Detour: quest -> BACK -> 7day -> mainstage...")
                                        self.run_stage6_detour("Before Stage 6")
                                        stage_notfound_count = 0

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
                                # 8-star heroes first every burst; the 6-star ones only every 3rd burst
                                strong_coords = [(483, 508), (582, 515), (683, 517), (146, 483)]
                                weak_coords = [(278, 521), (384, 514)]
                                strong_chain = " & ".join([f"input tap {c[0]} {c[1]}" for c in strong_coords * 2]) + " & wait"
                                weak_chain = " & ".join([f"input tap {c[0]} {c[1]}" for c in weak_coords]) + " & wait"
                                burst_n = 0
                                while not stop_spam:
                                    burst_n += 1
                                    chain = strong_chain if burst_n % 3 else f"{strong_chain}; {weak_chain}"
                                    subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", chain], **self.kwargs)
                                    # See TAP_BURST_INTERVAL: paces process spawns, not taps.
                                    time.sleep(TAP_BURST_INTERVAL)

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

                            # Check for an event popup for 5s before moving to the next stage
                            self.log(f"Stage {stage_num} done: checking for event (5s)...")
                            ev_end = time.time() + 5
                            while time.time() < ev_end:
                                self.capture_screen()
                                if self.handle_event_back():
                                    self.log(f"Cleared an event popup after stage {stage_num}.")
                                    break
                                time.sleep(0.5)
                        except StallDetected:
                            # A stall can land mid-battle, so stop the tap thread before recovering
                            stop_spam = True
                            try:
                                spam_thread.join(timeout=5)
                            except Exception:
                                pass
                            self.log(f"STALL on stage {stage_info['num']}: running RE-LOOP, then retrying this stage...")
                            self.handle_reloop()
                            continue
                        stage_idx += 1
                except StallDetected:
                    self.log(f"STALL outside the stage loop: running RE-LOOP, then retrying this account...")
                    self.handle_reloop()
                    continue
                except GameCrashed:
                    if not os.path.exists(os.path.join("backup", fname)):
                        self.log(f"RECOVERY: Account file {fname} no longer in backup/ (moved or removed). Switching to next account...")
                        self._release_file_lock(fname)
                        break
                    self.log(f"RECOVERY: Game crashed. Retrying account {fname} from login...")
                    time.sleep(2)
                    continue # Re-runs push, login, find_team, then repeat loop for same account
                except AccountFinished:
                    self.log(f"Account {self.current_account} finished (Stage 151 backed up or moved to lv5+). Switching...")
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

        # Make Team
        maketeam_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        maketeam_frame.pack(fill="x", padx=10, pady=5)
        self.var_maketeam = ctk.BooleanVar(value=bool(self.cfg.get("maketeam", 1)))
        ctk.CTkSwitch(maketeam_frame, text="จัดทีม (ปิด = ข้ามไปเล่นด่านเลย)", variable=self.var_maketeam).pack(side="left")

        
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
            self.cfg["maketeam"] = 1 if self.var_maketeam.get() else 0
            
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

    wg_startup(serials)

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
