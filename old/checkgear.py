import cv2
import numpy as np
import subprocess
import os
from time import sleep
import sys
import shutil
import glob
import tempfile
import json
import threading
import queue
import concurrent.futures
import colorama
from colorama import Fore, Style
import ssl

colorama.init(autoreset=True)

# Fix SSL certificate error for downloading EasyOCR models
ssl._create_default_https_context = ssl._create_unverified_context

# =============================================================
# Global Config
# =============================================================
config = {
    "first_loop": True,
    "thread_delay": 5,
    "check-gear": 1,
    "weaponname": {},
    "gearname": {},
    "ocr_region": {"x": 463, "y": 153, "w": 397, "h": 321}
}
adb_path = "adb"

# EasyOCR reader - loaded once globally
_ocr_reader = None
_ocr_lock = threading.Lock()  # Thread-safe OCR init

def get_ocr_reader():
    """Get or create EasyOCR reader (singleton, thread-safe)"""
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_lock:
            if _ocr_reader is None:  # Double-check after acquiring lock
                import easyocr
                print("[INFO] Loading EasyOCR model (first time only)...")
                _ocr_reader = easyocr.Reader(['en'], gpu=False)
                print("[OK] EasyOCR model loaded!")
    return _ocr_reader


def load_config():
    global config
    config_file = "checkgear_config.json"
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config.update(json.load(f))
            print("[OK] Config loaded:", json.dumps(config, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"[WARN] Error loading config: {e}")
    else:
        try:
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            print(f"[OK] Created default {config_file}")
        except:
            pass


def find_adb_executable():
    global adb_path
    
    if os.path.exists(r"adb\adb.exe"):
        adb_path = os.path.abspath(r"adb\adb.exe")
        print(f"[OK] Found local ADB: {adb_path}")
        try:
            ver = subprocess.check_output([adb_path, "version"], text=True)
            print(f"[DEBUG] {ver.strip()}")
        except Exception as e:
            print(f"[ERR] Failed to execute ADB: {e}")
            return False
        return True

    # Try system PATH - search for 'adb.exe' explicitly to avoid matching 'adb' folder
    import shutil
    adb_in_path = shutil.which("adb")
    if adb_in_path:
        adb_path = os.path.abspath(adb_in_path)
        print(f"[OK] Found ADB in PATH: {adb_path}")
        return True
    
    # Try common fallback "adb" string
    try:
        subprocess.run(["adb", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        adb_path = "adb"
        print("[OK] Found 'adb' command in system")
        return True
    except:
        pass

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
            print(f"[OK] Found MuMu ADB: {path}")
            return True
            
    print("[FAIL] ADB executable not found!")
    return False


def connect_known_ports():
    """Auto-scan and connect to common emulator ports using ThreadPoolExecutor"""
    print("[INFO] Auto-connecting to common emulator ports...")
    
    manual_ports = [62001, 21503, 7555]
    scan_range = [5555 + (i * 2) for i in range(725)]
    all_ports = sorted(list(set(manual_ports + scan_range)))
    
    print(f"[INFO] Scanning {len(all_ports)} ports...")

    def try_connect(port):
        target = f"127.0.0.1:{port}"
        cmd = [adb_path, "connect", target]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2, text=True)
            output = proc.stdout.strip()
            if "connected to" in output:
                 print(f"[OK] Connected to {target}")
            elif "refused" not in output and "cannot connect" not in output:
                 print(f"[DBG] {target} -> {output}")
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            print(f"[ERR] {target}: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        list(executor.map(try_connect, all_ports))
            
    print("[OK] Port scan finished.")


def get_connected_devices():
    try:
        adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
        cmd = f'{adb_cmd} devices'
        result = subprocess.check_output(cmd, shell=True, text=True)
        print(f"[DEBUG] Raw 'adb devices' output:\n{result}")
            
        lines = result.strip().split("\n")[1:]
        devices = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                if parts[0].startswith("127.0.0.1:"):
                    devices.append(parts[0])
        return devices
    except Exception as e:
        print(f"[FAIL] Error getting devices: {e}")
        return []


# =============================================================
# CheckGearBot Class
# =============================================================
class CheckGearBot(threading.Thread):
    def __init__(self, device_id, file_queue):
        threading.Thread.__init__(self)
        self.device_id = device_id
        self.file_queue = file_queue
        self.daemon = True
        
        safe_dev = device_id.replace(":", "_")
        self.filename = os.path.join(tempfile.gettempdir(), f"screen-{safe_dev}.png")
        self.first_loop_done = not config.get("first_loop", True)
        
        # Sequence Definitions (same as ranger.py)
        self.seq1 = ['icon.png', 'apple.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png']
        self.seq2 = ['check-gusetid.png', 'check-gusetid1.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png', 'check-ok1.png', 'check-ok2.png', 'check-ok3.png', 'check-ok4.png']
        
        self.adb_cmd = adb_path
        self._screen = None       # Cached screen image (grayscale for template matching)
        self._screen_color = None  # Cached screen image (color for OCR)
        
        # Load gear/weapon config
        self.gear_names = config.get("gearname", {})
        self.weapon_names = config.get("weaponname", {})
        self.ocr_region = config.get("ocr_region", {"x": 463, "y": 153, "w": 397, "h": 321})
        
        # Current processing file info
        self.current_original_filename = None

    def run(self):
        try:
            print(f"[{self.device_id}] CheckGear Bot Thread Started", flush=True)
            
            while True:
                if self.file_queue.empty():
                    print(f"[{self.device_id}] Queue is empty. Stopping thread.", flush=True)
                    break

                try:
                    # 0. Check First Loop
                    if not self.first_loop_done:
                        res = self.first_loop_process()
                        if res == "complete":
                            self.first_loop_done = True
                        else:
                            print(f"[{self.device_id}] First loop failed or incomplete. Retrying...")
                            sleep(2)
                            continue

                    # 1. Get File
                    try:
                        xml_file = self.file_queue.get(timeout=2)
                    except queue.Empty:
                        break
                    
                    self.current_original_filename = os.path.basename(xml_file)
                    print(f"[{self.device_id}] Processing file: {self.current_original_filename}")

                    # 2. Inject
                    injected_file = self.inject_file(xml_file)
                    
                    if injected_file:
                        # 2.5 Delete original from backup IMMEDIATELY after inject
                        #     to prevent other threads from picking it up
                        try:
                            if os.path.exists(xml_file):
                                os.remove(xml_file)
                                print(f"[{self.device_id}] Deleted from backup: {os.path.basename(xml_file)}")
                        except Exception as e:
                            print(f"[{self.device_id}] Error deleting from backup: {e}")
                        
                        # 3. Login (with check-gear after stoplogin)
                        status = self.main_login(injected_file)
                        
                        if status == "success":
                            # success already handled inside main_login -> process_check_gear
                            pass
                        elif status == "failed":
                            self.handle_failure(injected_file)
                            self.first_loop_done = False
                        else:
                            print(f"[{self.device_id}] Unknown status. Moving to next.")
                    else:
                        print(f"[{self.device_id}] Injection failed for {xml_file}")
                    
                    self.file_queue.task_done()
                    
                except Exception as e:
                    print(f"[{self.device_id}] Critical Thread Error: {e}", flush=True)
                    sleep(5)
        except Exception as e:
            print(f"[{self.device_id}] Thread Crash on Startup: {e}", flush=True)

    def handle_success(self, file_path):
        success_path = os.path.join(os.getcwd(), "login-success")
        if not os.path.exists(success_path): os.makedirs(success_path)
        
        print(f"[{self.device_id}] Login SUCCESS. Moving file.")
        dst = os.path.join(success_path, os.path.basename(file_path))
        try:
            shutil.move(file_path, dst)
        except Exception as e:
            print(f"[{self.device_id}] Error moving file: {e}")

    def handle_failure(self, file_path):
        failed_path = os.path.join(os.getcwd(), "login-failed")
        if not os.path.exists(failed_path): os.makedirs(failed_path)
        
        src_remote = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        dst_local = os.path.join(failed_path, os.path.basename(file_path))
        
        print(f"[{self.device_id}] Pulling failed file info...")
        
        temp_remote = f"/data/local/tmp/failed_pref_{self.device_id.replace(':','_')}.xml"
        self.adb_shell(f"su -c 'cp {src_remote} {temp_remote}'")
        self.adb_shell(f"su -c 'chmod 666 {temp_remote}'")
        self.adb_run([self.adb_cmd, "-s", self.device_id, "pull", temp_remote, dst_local])
        
        print(f"[{self.device_id}] Saved failed file to {dst_local}")
        
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"[{self.device_id}] Deleted original file from backup.")
        except Exception as e:
            print(f"[{self.device_id}] Error deleting original: {e}")

    # =========================================================
    # Interaction Methods (same as ranger.py)
    # =========================================================
    _template_cache = {}

    @classmethod
    def _get_template(cls, template_path):
        """Cache template images in RAM"""
        if template_path not in cls._template_cache:
            tpl = cv2.imread(template_path, 0)
            if tpl is not None:
                cls._template_cache[template_path] = tpl
            else:
                return None
        return cls._template_cache[template_path]
    
    def adb_run(self, args, timeout=10, **kwargs):
        """Run ADB command for this device"""
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, **kwargs)
    
    def adb_shell(self, shell_cmd, timeout=10):
        """Run ADB shell command for this device"""
        return subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", shell_cmd],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

    def capture_screen(self):
        """Capture screen and load into RAM (Robust version)"""
        # Clear previous screen to avoid using stale data if capture fails
        self._screen = None
        self._screen_color = None
        
        try:
            # Try fast method with increased timeout (20s)
            result = subprocess.run(
                [self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20
            )
            if result.stdout and len(result.stdout) > 100:
                buf = np.frombuffer(result.stdout, np.uint8)
                img_gray = cv2.imdecode(buf, 0)
                img_color = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if img_gray is not None:
                    self._screen = img_gray
                    self._screen_color = img_color
                    return True
        except Exception as e:
            print(f"[{self.device_id}] Fast capture error/timeout: {e}")
        
        # Fallback to slow but reliable method
        try:
            self.adb_shell("screencap -p /sdcard/screen.png", timeout=20)
            self.adb_run([self.adb_cmd, "-s", self.device_id, "pull", "/sdcard/screen.png", self.filename], timeout=20)
            if os.path.exists(self.filename):
                self._screen = cv2.imread(self.filename, 0)
                self._screen_color = cv2.imread(self.filename, cv2.IMREAD_COLOR)
                # Cleanup SD card
                self.adb_shell("rm /sdcard/screen.png")
                return self._screen is not None
        except Exception as e:
            print(f"[{self.device_id}] Fallback capture error: {e}")
            
        return False
    
    def _find_in_screen(self, template_path, similarity=0.8):
        """Find template in cached screen image (no new capture)"""
        if self._screen is None:
            return None
        template = self._get_template(template_path)
        if template is None:
            return None
        result = cv2.matchTemplate(self._screen, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(result >= similarity)
        if len(loc[0]) > 0:
            y, x = loc[0][0], loc[1][0]
            h, w = template.shape
            return x+w//2, y+h//2
        return None
    
    def find(self, template_path, similarity=0.8):
        """Capture + find"""
        self.capture_screen()
        return self._find_in_screen(template_path, similarity)
    
    def exists(self, template_path, similarity=0.8):
        return self.find(template_path, similarity) is not None

    def exists_in_cache(self, template_path, similarity=0.8):
        """Check if template exists in already-captured screen"""
        return self._find_in_screen(template_path, similarity) is not None

    def click(self, PSMRL, similarity=0.8):
        target = None
        if isinstance(PSMRL, str):
            if os.path.exists(PSMRL):
                target = self._find_in_screen(PSMRL, similarity)
                if target is None:
                    target = self.find(PSMRL, similarity)
        elif isinstance(PSMRL, tuple):
            target = PSMRL
            
        if target:
            x, y = target
            self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "input", "tap", str(x), str(y)])
            return True
        return False
    
    def tap(self, x, y):
        """Direct tap without image search"""
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "input", "tap", str(x), str(y)])
    
    def swipe(self, x1, y1, x2, y2, duration=300):
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", 
                     str(x1), str(y1), str(x2), str(y2), str(duration)])

    def check_error_images(self):
        """Check error images using cached screen"""
        if self._screen is None:
            return None
        # Common login errors
        if self.exists_in_cache(r"img\fixbuglogin.png") or \
           self.exists_in_cache(r"img\alert2.png") or \
           self.exists_in_cache(r"img\alert3.png"):
            return "fixbug"
            
        error_images = [r"img\failed1.png", r"img\fixalerterror1.png"]
        for err in error_images:
            if self.exists_in_cache(err):
                return err
        return None

    # =========================================================
    # OCR Methods - Read text from screen
    # =========================================================
    def ocr_read_region(self, x, y, w, h):
        """
        Read text from a specific region of the cached color screen using EasyOCR. (Optimized)
        Returns list of (text, confidence) tuples.
        """
        if self._screen_color is None:
            print(f"[{self.device_id}] No color screen captured for OCR!")
            return []
        
        # Crop region from color image
        img = self._screen_color[y:y+h, x:x+w]
        
        if img is None or img.size == 0:
            print(f"[{self.device_id}] OCR crop region empty!")
            return []
        
        # Reduced scaling (1.5x) for speed
        img = cv2.resize(img, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
        
        reader = get_ocr_reader()
        # Enable paragraph mode and performance flags
        results = reader.readtext(
            img, 
            detail=1, 
            paragraph=True,
            contrast_ths=0.1, 
            adjust_contrast=False,
            add_margin=0.1,
            width_ths=0.7
        )
        
        text_results = []
        for (bbox, text) in results:
            # Paragraph mode returns [(bbox, text), ...]
            text_results.append((text, 0.99))
        
        return text_results

    def ocr_read_full_screen(self):
        """
        Read all text from the full cached color screen.
        Returns combined text string.
        """
        if self._screen_color is None:
            return ""
        
        region = self.ocr_region
        return self.ocr_read_region(region["x"], region["y"], region["w"], region["h"])

    def check_gear_by_text(self):
        """
        Check gear by reading text from screen and matching against config gear names.
        Returns set of matched gear names.
        """
        print(f"[{self.device_id}] Reading screen text with OCR...")
        
        # Capture fresh screen
        self.capture_screen()
        
        # Read text from OCR region
        ocr_results = self.ocr_read_full_screen()
        
        if not ocr_results:
            print(f"[{self.device_id}] OCR returned no results")
            return set()
        
        # Combine all OCR text into one string (lowercase for matching)
        all_text = " ".join([text for text, conf in ocr_results]).lower()
        print(f"[{self.device_id}] OCR Text: {all_text}")
        
        # Match against gear names from config
        found_gears = set()
        for gear_key, gear_data in self.gear_names.items():
            # Support new format: {"ocr": "search text", "name": "custom name"}
            if isinstance(gear_data, dict):
                ocr_text = gear_data.get("ocr", "")
                custom_name = gear_data.get("name", ocr_text)
            else:
                # Fallback: old format where value is just a string
                ocr_text = gear_data
                custom_name = gear_data
            
            if ocr_text.lower() in all_text:
                print(f"[{self.device_id}] >> FOUND gear: '{ocr_text}' -> folder: '{custom_name}' (key: {gear_key})")
                found_gears.add(custom_name)
        
        return found_gears

    # =========================================================
    # Logic Methods
    # =========================================================
    def clear_specific_shared_prefs(self):
        """Delete specific shared_prefs files only (partial clear)"""
        base = "/data/data/com.linecorp.LGRGS/shared_prefs"
        # Files to delete to clear session but keep game data
        files_to_remove = [
            "_LINE_COCOS_PREF_KEY.xml",
            "com.linecorp.LGRGS.xml",
            "LINE_LGRGS_PREFS.xml",
            "NativeCache.xml",
            "LocalSettings.xml"
        ]
        
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(1)
        
        # Build rm commands for specific files
        rm_cmds = " && ".join([f"rm -f {base}/{f}" for f in files_to_remove])
        # Also include any .bak files to be safe
        rm_cmds += f" && rm -f {base}/*.bak"
        
        # We STOP deleting the entire cache and shared_prefs folder
        self.adb_shell(f"su -c '{rm_cmds}'")
        print(f"[{self.device_id}] Cleared specific shared_prefs (Partial)")

    def inject_file(self, local_xml_path):
        print(f"[{self.device_id}] Injecting file (Robust Mode)...")
        
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)
        
        self.adb_shell("su -c 'killall -9 com.linecorp.LGRGS 2>/dev/null || true'")
        sleep(1)

        src = os.path.abspath(local_xml_path)
        src_size = os.path.getsize(src)
        tmp = f"/data/local/tmp/temp_pref_{self.device_id.replace(':','_')}.xml"
        final_dir = "/data/data/com.linecorp.LGRGS/shared_prefs"
        final = f"{final_dir}/_LINE_COCOS_PREF_KEY.xml"
        
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # Clear previous artifacts
                self.adb_shell(f"su -c 'rm -f {final} && rm -f {tmp}'")
                
                # Push to tmp
                self.adb_run([self.adb_cmd, "-s", self.device_id, "push", src, tmp], timeout=30, check=True)
                
                # Verify file size remotely
                size_check = self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", f"stat -c %s {tmp}"], text=True)
                remote_size_str = size_check.stdout.strip()
                remote_size = int(remote_size_str) if remote_size_str.isdigit() else 0
                
                if remote_size != src_size:
                    print(f"[{self.device_id}] Size mismatch! Local:{src_size} Remote:{remote_size} (Attempt {attempt})")
                    sleep(1)
                    continue
                
                # Robust deployment shell command
                shell_cmd = (
                    f"su -c '"
                    f"mkdir -p {final_dir} && "
                    f"cp -f {tmp} {final} && "
                    f"chmod 666 {final} && "
                    f"chown $(stat -c %u:%g {final_dir}) {final} || true && "
                    f"restorecon {final} || true && "
                    f"rm -f {tmp}"
                    f"'"
                )
                self.adb_shell(shell_cmd)
                
                # Final verification
                verify = self.adb_run(
                    [self.adb_cmd, "-s", self.device_id, "shell", f"su -c 'stat -c %s {final}'"], text=True
                )
                final_size_str = verify.stdout.strip()
                final_size = int(final_size_str) if final_size_str.isdigit() else 0
                
                if final_size == src_size:
                    print(f"[{self.device_id}] Injection Verified OK (Size: {final_size} bytes)")
                    return local_xml_path
                else:
                    print(f"[{self.device_id}] Verification FAILED! Expected:{src_size} Got:{final_size} (Attempt {attempt})")
                    sleep(1)
                    
            except Exception as e:
                print(f"[{self.device_id}] Injection Exception (Attempt {attempt}): {e}")
                sleep(1)
        
        print(f"[{self.device_id}] Injection FAILED after {max_retries} attempts!")
        return None

    def first_loop_process(self):
        try:
            print(f"[{self.device_id}] Starting First Loop Process...")
            self.clear_specific_shared_prefs()
            sleep(3)
            
            self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
            sleep(1)
            self.adb_shell("input keyevent 3")  # Home
            sleep(2)
            
            print(f"[{self.device_id}] Processing SEQ 1...")
            if not self.process_sequence(self.seq1): return "failed_seq1"
            
            print(f"[{self.device_id}] Waiting 8s then Back...")
            sleep(8)
            self.adb_shell("input keyevent 4")
            sleep(2)
            
            print(f"[{self.device_id}] Processing SEQ 2...")
            if not self.process_sequence(self.seq2): return "failed_seq2"
            
            print(f"[{self.device_id}] First Loop Completed!")
            self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
            sleep(2)
            return "complete"
            
        except Exception as e:
            print(f"[{self.device_id}] First Loop Error: {e}")
            return "error"

    def _check_and_click_icon(self):
        """Check if icon.png or fixalerterror1.png is on screen and click it"""
        if self.exists_in_cache(r"img\icon.png"):
            print(f"[{self.device_id}] Found icon.png! Clicking to relaunch...")
            self.click(r"img\icon.png")
            sleep(5)
            return True
        if self.exists_in_cache(r"img\fixalerterror1.png"):
            print(f"[{self.device_id}] Found fixalerterror1.png! Clicking to dismiss...")
            self.click(r"img\fixalerterror1.png")
            sleep(2)
            return True
        if self.exists_in_cache(r"img\fixplay.png"):
            print(f"[{self.device_id}] Found fixplay.png! Clicking...")
            self.click(r"img\fixplay.png")
            sleep(1)
            return True
        if self.exists_in_cache(r"img\alert2.png"):
            print(f"[{self.device_id}] Found alert2.png! Clicking and waiting 50s...")
            self.click(r"img\alert2.png")
            sleep(50)
            return True
        if self.exists_in_cache(r"img\alert3.png"):
            print(f"[{self.device_id}] Found alert3.png! Clicking and waiting 50s...")
            self.click(r"img\alert3.png")
            sleep(50)
            return True
        return False

    def process_sequence(self, sequence):
        for item in sequence:
            if isinstance(item, tuple):
                print(f"[{self.device_id}] Tap {item}")
                self.click(item)
                sleep(2)
                continue
            
            if isinstance(item, str) and item.startswith('@'):
                checkpoint_img = item[1:]
                print(f"[{self.device_id}] Waiting for checkpoint {checkpoint_img}...")
                while True:
                    self.capture_screen()
                    self._check_and_click_icon()
                    if self.exists_in_cache(f"img\\{checkpoint_img}"):
                        print(f"[{self.device_id}] Checkpoint {checkpoint_img} found!")
                        break
                    if self.check_error_images():
                        print(f"[{self.device_id}] Bug found during checkpoint! Restarting...")
                        return False
                    sleep(1)
                continue
                
            img = item
            if img == 'icon.png':
                print(f"[{self.device_id}] Waiting for {img}...")
                wait_limit = 60
                start_wait = 0
                found = False
                while start_wait < wait_limit:
                    loc = self.find(f"img\\{img}")
                    if loc:
                        self.click(loc)
                        print(f"[{self.device_id}] Clicked {img}")
                        sleep(5)
                        found = True
                        break
                    sleep(1)
                    start_wait += 1
                if not found:
                    print(f"[{self.device_id}] Failed to find {img}. Sequence broken.")
                    return False
                continue

            print(f"[{self.device_id}] Waiting for {img}...")
            
            wait_limit = 60
            start_wait = 0
            found = False
            
            while start_wait < wait_limit:
                self.capture_screen()
                
                # 1. Prioritize target image
                loc = self._find_in_screen(f"img\\{img}")
                if loc:
                    self.click(loc)
                    print(f"[{self.device_id}] Clicked {img}")
                    if img == 'apple.png':
                        sleep(1) # Fast
                    else:
                        sleep(6)
                    found = True
                    break 

                # 2. Check for bugs
                self._check_and_click_icon()
                
                if self.check_error_images():
                    print(f"[{self.device_id}] Bug detected! Waiting 50s...")
                    return False
                
                sleep(1)
                start_wait += 1
            
            if not found:
                 print(f"[{self.device_id}] Failed to find {img}. Sequence broken.")
                 return False
                 
        return True

    # =========================================================
    # CHECK GEAR Process - Runs after stoplogin found
    # =========================================================
    def wait_and_click_image(self, img_name, timeout=60):
        """Wait for image to appear and click it. Returns True if found."""
        print(f"[{self.device_id}] Waiting for {img_name}...")
        start = 0
        while start < timeout:
            try:
                self.capture_screen()
                
                # Check error/crash
                self._check_and_click_icon()
                if self.check_error_images():
                    print(f"[{self.device_id}] Error found while waiting for {img_name}")
                    return False
                
                loc = self._find_in_screen(f"img\\{img_name}")
                if loc:
                    print(f"[{self.device_id}] Found {img_name} at {loc} - clicking")
                    self.click(loc)
                    sleep(1.5)
                    return True
                sleep(0.5)
                start += 1
            except Exception as e:
                print(f"[{self.device_id}] Error finding {img_name}: {e}")
                sleep(1)
                start += 1
        print(f"[{self.device_id}] Timeout waiting for {img_name}")
        return False

    def process_check_gear(self, current_file):
        """
        Process check-gear sequence.
        1) Navigate to gear screen (findgear1 -> findgear2 -> findgear3)
        2) Read gear names via OCR text matching
        3) Check weapon tabs (weapons1, weapons2) for more gears
        4) Backup with gear name prefix
        5) Clear app and restart
        """
        print(f"\n[{self.device_id}] === Starting CHECK-GEAR Process ===\n")
        
        # Step 1: Navigate to gear pages
        # Build filename and source for not-found backup
        filename = self.current_original_filename or "unknown_LINE_COCOS_PREF_KEY.xml"
        source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        
        # Click findgear1.png
        if not self.wait_and_click_image("findgear1.png"):
            print(f"[{self.device_id}] Failed to find findgear1.png, skipping check-gear")
            self.adb_shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
            self.adb_shell(f"su -c 'chmod 777 {source_path}'")
            self.backup_to_not_found(filename, source_path)
            self.clear_and_restart()
            return "success"
        
        # Click findgear2.png
        if not self.wait_and_click_image("findgear2.png"):
            print(f"[{self.device_id}] Failed to find findgear2.png, skipping check-gear")
            self.adb_shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
            self.adb_shell(f"su -c 'chmod 777 {source_path}'")
            self.backup_to_not_found(filename, source_path)
            self.clear_and_restart()
            return "success"
        
        # Click findgear3.png
        if not self.wait_and_click_image("findgear3.png"):
            print(f"[{self.device_id}] Failed to find findgear3.png, skipping check-gear")
            self.adb_shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
            self.adb_shell(f"su -c 'chmod 777 {source_path}'")
            self.backup_to_not_found(filename, source_path)
            self.clear_and_restart()
            return "success"
        
        # Click checkgear2.png
        if not self.wait_and_click_image("checkgear2.png"):
            print(f"[{self.device_id}] Failed to find checkgear2.png, skipping")
        
        # Click checkgear3.png
        if not self.wait_and_click_image("checkgear3.png"):
            print(f"[{self.device_id}] Failed to find checkgear3.png, skipping")
        
        # Step 2: Read gear names with OCR
        print(f"\n[{self.device_id}] Starting gear OCR check...")
        all_found_gears = set()
        
        # Round 1: Check gear directly on current screen
        print(f"[{self.device_id}] Round 1: Direct OCR check")
        all_found_gears.update(self.check_gear_by_text())
        sleep(3)
        
        # Round 2: Check weapons tab 1
        self.capture_screen()
        if self.exists_in_cache(r"img\weapons1.png"):
            print(f"\n[{self.device_id}] Round 2: Checking after weapons1.png")
            self.click(r"img\weapons1.png")
            sleep(2)
            all_found_gears.update(self.check_gear_by_text())
            sleep(3)
        
        # Round 3: Check weapons tab 2
        self.capture_screen()
        if self.exists_in_cache(r"img\weapons2.png"):
            print(f"\n[{self.device_id}] Round 3: Checking after weapons2.png")
            self.click(r"img\weapons2.png")
            sleep(2)
            all_found_gears.update(self.check_gear_by_text())
            sleep(3)
        
        # Step 3: Backup
        print(f"\n[{self.device_id}] Starting backup...")
        
        # Build filename (use original name)
        filename = self.current_original_filename or "unknown_LINE_COCOS_PREF_KEY.xml"
        source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        
        # chmod for pull
        self.adb_shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
        self.adb_shell(f"su -c 'chmod 777 {source_path}'")
        
        if all_found_gears:
            # Build folder name: single gear = "lapel", multiple = "lapel+uniform-anya"
            gear_folder_name = "+".join(sorted(all_found_gears))
            
            print(f"[{self.device_id}] Found gears: {', '.join(all_found_gears)}")
            print(f"[{self.device_id}] Folder: backup-id/{gear_folder_name}/")
            
            # Create backup-id/gear_folder/
            gear_dir = os.path.join("backup-id", gear_folder_name)
            if not os.path.exists(gear_dir):
                os.makedirs(gear_dir)
                print(f"[{self.device_id}] Created folder: {gear_dir}")
            
            # Pull directly to gear folder
            dst = os.path.join(gear_dir, filename)
            result = subprocess.run(
                [self.adb_cmd, '-s', self.device_id, 'pull', source_path, dst],
                capture_output=True, text=True
            )
            
            if result.returncode == 0:
                print(f"[{self.device_id}] Backup SUCCESS: {dst}")
            else:
                print(f"[{self.device_id}] Backup FAILED: {result.stderr}")
                self.backup_to_not_found(filename, source_path)
        else:
            print(f"[{self.device_id}] No gear found - backup to not-found")
            self.backup_to_not_found(filename, source_path)
        
        # Step 4: Clear app and restart
        print(f"\n[{self.device_id}] Check-gear complete - clearing app and restarting")
        self.clear_and_restart()
        return "success"

    def backup_to_not_found(self, filename, source_path):
        """Backup pref file to not-found folder"""
        not_found_dir = "not-found"
        if not os.path.exists(not_found_dir):
            os.makedirs(not_found_dir)
        
        backup_path = os.path.join(not_found_dir, filename)
        
        result = subprocess.run(
            [self.adb_cmd, '-s', self.device_id, 'pull', source_path, backup_path],
            capture_output=True, text=True
        )
        
        if result.returncode == 0:
            print(f"[{self.device_id}] Saved to not-found: {backup_path}")
        else:
            print(f"[{self.device_id}] not-found backup error: {result.stderr}")

    def clear_and_restart(self):
        """Clear app and prepare for next file"""
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)

    # =========================================================
    # Main Login - Modified: After stoplogin, run check-gear
    # =========================================================
    def main_login(self, current_filename):
        print(f"[{self.device_id}] Starting Main Login...")
        
        # Clear app
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)
        
        # Click icon if found
        if self.exists(r"img\icon.png"):
            self.click(r"img\icon.png")
            sleep(5)
            
        loop_count = 0
        status = "unknown"
        
        while True:
            loop_count += 1
            if loop_count % 5 == 0:
                print(f"[{self.device_id}] Login Loop #{loop_count}")

            self.capture_screen()

            # Fix/Alert checks
            if self._check_and_click_icon():
                loop_count = 0
                continue
                
            # Success
            if self.exists_in_cache(r"img\stoplogin.png"):
                print(f"[{self.device_id}] Found stoplogin (Success)")
                status = "success"
                # Proceed to gear check process
                self.process_check_gear(current_filename)
                break
                
            # Failed
            if self.exists_in_cache(r"img\login-failed.png"):
                print(f"[{self.device_id}] Found login-failed. Executing recovery sequence...")
                
                # 1. Click login-failed1
                self.capture_screen()
                if self.exists_in_cache(r"img\login-failed1.png"):
                    self.click(r"img\login-failed1.png")
                    sleep(2)
                
                if self.process_sequence(self.seq1[1:]):
                    print(f"[{self.device_id}] SEQ1 done. Waiting 8s then Back...")
                    sleep(8)
                    self.adb_shell("input keyevent 4")
                    sleep(2)
                    
                    print(f"[{self.device_id}] Processing SEQ 2...")
                    self.process_sequence(self.seq2)
                
                self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
                status = "failed"
                break
                
            # Error/Reset (failed1, fixbuglogin) - already checked from cached screen
            error_found = self.check_error_images()
            
            if error_found:
                print(f"[{self.device_id}] Found {error_found}. Resetting...")
                if error_found == "fixbug":
                    if self.exists_in_cache(r"img\alert2.png"): self.click(r"img\alert2.png")
                    elif self.exists_in_cache(r"img\alert3.png"): self.click(r"img\alert3.png")
                    else: self.click(r"img\fixbuglogin.png")
                    print(f"[{self.device_id}] fixbug/alert detected, clicking and waiting 50s...")
                    sleep(50)
                 
                self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
                sleep(2)
                self.adb_shell("input keyevent 3")
                sleep(2)
                if self.exists(r"img\icon.png"):
                    self.click(r"img\icon.png")
                    sleep(5)
                continue
            
            # Event
            if self.exists_in_cache(r"img\event.png"):
                print(f"[{self.device_id}] Handling Event...")
                self.click(r"img\event.png")
                sleep(1)
                
                back_attempts = 0
                while True:
                    self.adb_shell("input keyevent 4 && input keyevent 4 && input keyevent 4")
                    back_attempts += 3
                    
                    if back_attempts % 9 == 0:
                        self.capture_screen()
                        if self.exists_in_cache(r"img\cancel.png"):
                            self.click(r"img\cancel.png")
                            break
                        if self.exists_in_cache(r"img\stoplogin.png"):
                            # Found stoplogin during event handling -> check gear
                            print(f"[{self.device_id}] Found stoplogin during event! -> check-gear")
                            if config.get("check-gear", 0) == 1:
                                return self.process_check_gear(current_filename)
                            status = "success"
                            break
                    
                    if back_attempts > 60: break
                    sleep(0.5)
                
                if status == "success": break
            
            sleep(2)
            if loop_count > 500:
                print(f"[{self.device_id}] Max loops reached.")
                break
        
        # Cleanup
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        return status


# =============================================================
# Main Entry Point
# =============================================================
if __name__ == "__main__":
    print("=== Auto ADB Check-Gear Script (Multi-Threaded) ===")
    
    load_config()
    
    if not find_adb_executable():
        print("ADB Not Found.")
        sys.exit(1)
    
    # Reset ADB
    print("[INFO] Restarting ADB Server...")
    subprocess.run([adb_path, "kill-server"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run([adb_path, "start-server"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
    connect_known_ports()
    all_detected = get_connected_devices()
    devices = [d for d in all_detected if d.startswith("emulator-")]
    print(f"[DEV] Using emulators: {devices}")
    
    if not devices:
        print("No devices.")
        sys.exit(0)
    
    # Pre-load OCR model
    print("[INFO] Pre-loading OCR model...")
    try:
        get_ocr_reader()
        print("[OK] OCR model loaded.")
    except Exception as e:
        print(f"[WARN] Failed to load OCR: {e}")
        print("[WARN] OCR will be retried when needed.")
        
    # Prepare Queue
    file_queue = queue.Queue()
    backup_path = os.path.join(os.getcwd(), "backup")
    
    if os.path.exists(backup_path):
        files = glob.glob(os.path.join(backup_path, "*.xml"))
        for f in files:
            file_queue.put(f)
        print(f"[FILE] Loaded {len(files)} files into queue.")
    else:
        print("[WARN] No backup folder.")
        
    # Print gear config info
    gear_names = config.get("gearname", {})
    weapon_names = config.get("weaponname", {})
    print(f"\n[CONFIG] Gear names to check ({len(gear_names)}):")
    for k, v in gear_names.items():
        if isinstance(v, dict):
            print(f"  {k}: OCR='{v.get('ocr','')}' -> Folder='{v.get('name','')}'")
        else:
            print(f"  {k}: {v}")
    print(f"[CONFIG] Weapon tabs ({len(weapon_names)}):")
    for k, v in weapon_names.items():
        print(f"  {k}: {v}")
    print()
    
    # Start Threads
    threads = []
    print(f"[INFO] Starting {len(devices)} threads...")
    
    delay = config.get("thread_delay", 5)
    for i, dev in enumerate(devices):
        t = CheckGearBot(dev, file_queue)
        t.start()
        threads.append(t)
        if i < len(devices) - 1:
            print(f"[INFO] Waiting {delay}s before starting next thread...")
            sleep(delay)
        
    # Wait for threads
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[STOP] Keyboard Interrupt. Stopping...")
        
    print("\n[DONE] All tasks completed.")
