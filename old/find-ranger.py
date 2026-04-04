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

colorama.init(autoreset=True)

# =============================================================
# Global Config
# =============================================================
config = {
    "first_loop": True,
    "thread_delay": 5,
    "custommode": 0,
    "custom": {"characters": []},
    "characters": [],
    "ranger_images": []
}
adb_path = "adb"


def load_config():
    global config
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "find-ranger_config.json")
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
            config.update(loaded)
        print(f"[CONFIG] Loaded: {config_file}")
    else:
        print(f"[WARN] Config not found: {config_file}")


def find_adb_executable():
    global adb_path
    
    # Check common locations
    script_dir = os.path.dirname(os.path.abspath(__file__))
    adb_locations = [
        os.path.join(script_dir, "adb", "adb.exe"),
        os.path.join(script_dir, "adb", "adb"),
        "adb",
    ]
    
    for loc in adb_locations:
        try:
            result = subprocess.run(
                [loc, "version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                adb_path = loc
                print(f"[ADB] Found: {adb_path}")
                return True
        except:
            continue
    
    # Try system PATH - search for 'adb.exe' explicitly to avoid matching 'adb' folder
    import shutil
    adb_in_path = shutil.which("adb")
    if adb_in_path:
        adb_path = os.path.abspath(adb_in_path)
        print(f"[ADB] Found in PATH: {adb_path}")
        return True
    
    # Try common fallback "adb" string
    try:
        subprocess.run(["adb", "--version"], capture_output=True, timeout=5, check=True)
        adb_path = "adb"
        print(f"[ADB] Found 'adb' command in system")
        return True
    except:
        pass
    
    return False


def connect_known_ports():
    """Auto-scan and connect to common emulator ports using ThreadPoolExecutor"""
    # Optimized scan for odd ports
    ports = [5555 + (i * 2) for i in range(725)] + [7555, 62001, 62025, 62026, 21503, 21513, 21523, 21533]
    print(f"[INFO] Fast scanning {len(ports)} ports...")

    def try_connect(port):
        addr = f"127.0.0.1:{port}"
        try:
            result = subprocess.run(
                [adb_path, "connect", addr],
                capture_output=True, text=True, timeout=2
            )
            out = result.stdout.strip().lower()
            if "connected" in out and "cannot" not in out:
                print(f"[OK] Connected: {addr}")
                return addr
        except:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        futures = {executor.submit(try_connect, port): port for port in ports}
        for future in concurrent.futures.as_completed(futures):
            future.result()


def get_connected_devices():
    try:
        result = subprocess.run(
            [adb_path, "devices"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")[1:]
        devices = []
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except Exception as e:
        print(f"[ERR] get_connected_devices: {e}")
        return []


# =============================================================
# FindRangerBot Class
# =============================================================
class FindRangerBot(threading.Thread):
    def __init__(self, device_id, file_queue):
        threading.Thread.__init__(self)
        self.device_id = device_id
        self.file_queue = file_queue
        self.daemon = True
        
        # Unique filename for this thread
        safe_dev = device_id.replace(":", "_")
        self.filename = os.path.join(tempfile.gettempdir(), f"screen-{safe_dev}.png")
        self.first_loop_done = not config.get("first_loop", True)
        
        # Load character list based on mode (custommode=1 for custom list)
        if config.get("custommode") == 1:
            custom_data = config.get("custom", {})
            self.characters = custom_data.get("characters", [])
            print(f"[{self.device_id}] Custom mode (custommode=1) -> searching: {self.characters}")
        else:
            self.characters = config.get("characters", [])
            print(f"[{self.device_id}] Find-all mode -> searching {len(self.characters)} characters")
        
        # Auto-scan img/ranger/ folder for all png files
        self.ranger_image_mapping = config.get("ranger_images", {})
        ranger_folder = os.path.join("img", "ranger")
        self.ranger_files = []
        if os.path.exists(ranger_folder):
            for f in sorted(os.listdir(ranger_folder)):
                if f.lower().endswith(".png"):
                    self.ranger_files.append(f"ranger/{f}")
            print(f"[{self.device_id}] Auto-loaded {len(self.ranger_files)} ranger images from img/ranger/")
        
        # Store original filename for backup
        self.current_original_filename = None
        
        # Sequence Definitions (prefix @ = checkpoint: wait until found but don't click)
        self.seq1 = ['icon.png', 'apple.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png']
        self.seq2 = ['check-gusetid.png', 'check-gusetid1.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png', 'check-ok1.png', 'check-ok2.png', 'check-ok3.png', 'check-ok4.png']
        
        self.adb_cmd = adb_path
        self._screen = None
        self._template_cache = {}

    def run(self):
        try:
            print(f"[{self.device_id}] FindRanger Bot Thread Started", flush=True)
            
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
                    
                    # Store original filename
                    self.current_original_filename = os.path.basename(xml_file)
                    print(f"[{self.device_id}] Processing file: {self.current_original_filename}")

                    # 2. Inject
                    injected_file = self.inject_file(xml_file)
                    
                    if injected_file:
                        # 2.5 Delete original from backup IMMEDIATELY
                        try:
                            if os.path.exists(xml_file):
                                os.remove(xml_file)
                                print(f"[{self.device_id}] Deleted from backup: {self.current_original_filename}")
                        except Exception as e:
                            print(f"[{self.device_id}] Error deleting from backup: {e}")
                        
                        # 3. Login (after stoplogin -> find ranger flow)
                        status = self.main_login(injected_file)
                        
                        if status == "success":
                            pass  # handled inside main_login -> process_find_ranger
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

    # =========================================================
    # File Handling
    # =========================================================
    def handle_success(self, file_path):
        dst_dir = "login-success"
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir)
        base = os.path.basename(file_path)
        dst = os.path.join(dst_dir, base)
        try:
            shutil.move(file_path, dst)
            print(f"[{self.device_id}] Moved to {dst_dir}: {base}")
        except Exception as e:
            print(f"[{self.device_id}] Move error: {e}")

    def handle_failure(self, file_path):
        dst_dir = "login-failed"
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir)
        base = os.path.basename(file_path)
        dst = os.path.join(dst_dir, base)
        try:
            if os.path.exists(file_path):
                shutil.move(file_path, dst)
                print(f"[{self.device_id}] Moved to {dst_dir}: {base}")
        except Exception as e:
            print(f"[{self.device_id}] Move error: {e}")

    # =========================================================
    # Screen & Image Methods  
    # =========================================================
    @classmethod
    def _get_template(cls, template_path):
        if template_path not in cls.__dict__.get('_template_cache_cls', {}):
            if not hasattr(cls, '_template_cache_cls'):
                cls._template_cache_cls = {}
            tmpl = cv2.imread(template_path, 0)
            cls._template_cache_cls[template_path] = tmpl
        return cls._template_cache_cls[template_path]

    def adb_run(self, args, timeout=10, **kwargs):
        return subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)

    def adb_shell(self, shell_cmd, timeout=10):
        return subprocess.run(
            [self.adb_cmd, "-s", self.device_id, "shell", shell_cmd],
            capture_output=True, timeout=timeout)

    def capture_screen(self):
        """Capture screen and load into RAM (Robust version)"""
        # Clear previous screen to avoid using stale data if capture fails
        self._screen = None
        self._screen_color = None
        
        try:
            # Try fast method with increased timeout (20s)
            result = subprocess.run(
                [self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=20
            )
            if result.returncode == 0 and len(result.stdout) > 100:
                img_array = np.frombuffer(result.stdout, np.uint8)
                self._screen = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
                self._screen_color = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                return True
        except Exception as e:
            print(f"[{self.device_id}] Fast capture error/timeout: {e}")
        
        # Fallback to slow but reliable method
        try:
            # Use self.filename as temp storage
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
        tmpl = self._get_template(template_path)
        if tmpl is None:
            return None
        try:
            result = cv2.matchTemplate(self._screen, tmpl, cv2.TM_CCOEFF_NORMED)
            loc = np.where(result >= similarity)
            if len(loc[0]) > 0:
                y, x = loc[0][0], loc[1][0]
                h, w = tmpl.shape
                return (x + w // 2, y + h // 2)
        except:
            pass
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

    def _get_similarity_score(self, template_path):
        """Get max similarity score for template in cached screen"""
        if self._screen is None:
            return 0.0
        tmpl = self._get_template(template_path)
        if tmpl is None:
            return 0.0
        try:
            result = cv2.matchTemplate(self._screen, tmpl, cv2.TM_CCOEFF_NORMED)
            return float(np.max(result))
        except:
            return 0.0
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

    def type_text(self, text):
        """Type text via ADB (for search box)"""
        # Escape special chars for shell
        escaped = text.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "input", "text", escaped])

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
            self.adb_shell("input keyevent 3")
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
                
                # 1. Prioritize target image (Work immediately!)
                loc = self._find_in_screen(f"img\\{img}")
                if loc:
                    self.click(loc)
                    print(f"[{self.device_id}] Clicked {img}")
                    if img == 'apple.png':
                        sleep(1) # Fast work
                    else:
                        sleep(6)
                    found = True
                    break 

                # 2. Casually check for icons/bugs
                self._check_and_click_icon()
                
                if self.check_error_images():
                    print(f"[{self.device_id}] Bug detected after clicking! Waiting 50s...")
                    # The clicking is handled in main_login or _check_and_click_icon
                    return False

                sleep(1)
                start_wait += 1
            
            if not found:
                 print(f"[{self.device_id}] Failed to find {img}. Sequence broken.")
                 return False
                 
        return True

    # =========================================================
    # Wait and Click helper
    # =========================================================
    def wait_and_click_image(self, img_name, timeout=30):
        """Wait for image and click it, return True if found (timeout in seconds)"""
        start = 0
        while start < timeout:
            try:
                self.capture_screen()
                loc = self._find_in_screen(f"img\\{img_name}")
                if loc:
                    print(f"[{self.device_id}] Found {img_name} - clicking")
                    self.click(loc)
                    sleep(0.5)
                    return True
                
                # Check for crash/close while waiting
                if self.exists_in_cache(r"img\icon.png"):
                    print(f"[{self.device_id}] App crashed while waiting for {img_name}")
                    return False

                sleep(1)
                start += 1
            except Exception as e:
                print(f"[{self.device_id}] Error finding {img_name}: {e}")
                sleep(1)
                start += 1
        print(f"[{self.device_id}] Timeout waiting for {img_name} ({timeout}s)")
        return False

    # =========================================================
    # FIND RANGER PROCESS - Main Feature
    # =========================================================
    def process_find_ranger(self, current_file):
        """
        After successful login (stoplogin detected):
        1) Click sec1 -> sec2
        2) For each character in config:
           a) Type character name to search
           b) Click sec3 -> sec4
           c) Scan for ranger images (ranger1, ranger2, ranger3)
           d) Store found names
           e) Click sec5
           f) Repeat for next character
        3) Print results and backup
        """
        print(f"\n[{self.device_id}] === Starting FIND-RANGER Process ===\n")
        
        # Results storage: {character_name: [found_ranger_images]}
        results = {}
        
        # Step 1 & 2: REPEAT FOREVER Navigation until Search Screen reached
        print(f"[{self.device_id}] Starting persistent navigation (Searching for sec1/sec2)...")
        while True:
            self.capture_screen()
            
            # Check for crash/close while waiting
            if self.exists_in_cache(r"img\icon.png"):
                print(f"[{self.device_id}] App closed during navigation. Relaunching...")
                self.click(r"img\icon.png")
                sleep(5)
                continue

            # Check if we are already at sec2 (sometimes transition is fast or already there)
            if self.exists_in_cache(r"img\sec2.png"):
                print(f"[{self.device_id}] Found sec2.png! Entry confirmed.")
                self.click(r"img\sec2.png")
                break
                
            # Try clicking sec1
            if self.exists_in_cache(r"img\sec1.png"):
                print(f"[{self.device_id}] Found sec1.png! Clicking...")
                self.click(r"img\sec1.png")
                sleep(3) # Wait for sec2 to appear
                continue
            
            # If nothing found, just wait and loop again
            sleep(1.5)
            
        print(f"[{self.device_id}] Reached search screen successfully.")
        sleep(0.5)
        
        # Loop through each character
        for i, character in enumerate(self.characters):
            print(f"\n[{self.device_id}] --- Character {i+1}/{len(self.characters)}: {character} ---")
            
            # a) Tap search box position first
            print(f"[{self.device_id}] Tapping search box (388, 288)")
            self.tap(388, 288)
            sleep(0.3)
            
            # b) Type character name
            print(f"[{self.device_id}] Typing: {character}")
            self.type_text(character)
            sleep(0.5)
            
            # b) Click sec3
            print(f"[{self.device_id}] Clicking sec3.png")
            if not self.wait_and_click_image("sec3.png", timeout=15):
                print(f"[{self.device_id}] sec3.png not found, skipping {character}")
                continue
            sleep(0.3)
            
            # c) Click sec4
            print(f"[{self.device_id}] Clicking sec4.png")
            if not self.wait_and_click_image("sec4.png", timeout=15):
                print(f"[{self.device_id}] sec4.png not found, skipping {character}")
                continue
            sleep(0.3)
            
            # d) Scan ALL ranger images from img/ranger/ folder
            self.capture_screen()
            
            for ranger_img in self.ranger_files:
                img_path = f"img\\{ranger_img}"
                if self.exists_in_cache(img_path, similarity=0.95):
                    if ranger_img in self.ranger_image_mapping:
                        data = self.ranger_image_mapping[ranger_img]
                        hero_name = data.get("hero", ranger_img)
                        folder_name = data.get("folder", hero_name)
                    else:
                        hero_name = os.path.splitext(os.path.basename(ranger_img))[0]
                        folder_name = hero_name
                    print(f"[{self.device_id}]   >> FOUND: {ranger_img} -> {hero_name}")
                    results[hero_name] = folder_name
            
            if results:
                print(f"[{self.device_id}]   Results so far: {list(results.keys())}")
            else:
                print(f"[{self.device_id}]   No ranger images found for {character}")
            
            # e) Click sec5
            print(f"[{self.device_id}] Clicking sec5.png")
            if not self.wait_and_click_image("sec5.png", timeout=15):
                print(f"[{self.device_id}] sec5.png not found")
            sleep(0.3)
            
            # f) Click sec2 again for next character (if not last)
            if i < len(self.characters) - 1:
                print(f"[{self.device_id}] Clicking sec2.png for next character")
                if not self.wait_and_click_image("sec2.png", timeout=15):
                    print(f"[{self.device_id}] sec2.png not found, stopping loop")
                    break
                sleep(0.3)
        
        # Print final results
        print(f"\n[{self.device_id}] ========== FIND-RANGER RESULTS ==========")
        print(f"[{self.device_id}] File: {self.current_original_filename}")
        if results:
            for hero_name, folder_name in results.items():
                print(f"[{self.device_id}]   {hero_name} -> folder: {folder_name}")
        else:
            print(f"[{self.device_id}]   No rangers found for any character")
        print(f"[{self.device_id}] ==========================================\n")
        
        # Backup results
        self.backup_find_ranger_results(results)
        
        # Clear app and restart
        print(f"\n[{self.device_id}] Find-Ranger complete - clearing app")
        self.clear_and_restart()
        return "success"

    def backup_find_ranger_results(self, results):
        """Save backup based on find-ranger results"""
        filename = self.current_original_filename or "unknown.xml"
        source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        
        self.adb_shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
        self.adb_shell(f"su -c 'chmod 777 {source_path}'")
        
        if results:
            # Build folder name from folder values (e.g. "Anya+Yor")
            folder_parts = sorted(results.values())
            folder_name = "+".join(folder_parts)
            
            backup_dir = os.path.join("backup-id", folder_name)
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir)
                print(f"[{self.device_id}] Created folder: {backup_dir}")
            
            dst = os.path.join(backup_dir, filename)
            result = subprocess.run(
                [self.adb_cmd, '-s', self.device_id, 'pull', source_path, dst],
                capture_output=True, text=True
            )
            
            if result.returncode == 0:
                print(f"[{self.device_id}] Backup SUCCESS: {dst}")
            else:
                print(f"[{self.device_id}] Backup FAILED: {result.stderr}")
        else:
            # No results -> not-found
            not_found_dir = "not-found"
            if not os.path.exists(not_found_dir):
                os.makedirs(not_found_dir)
            
            dst = os.path.join(not_found_dir, filename)
            result = subprocess.run(
                [self.adb_cmd, '-s', self.device_id, 'pull', source_path, dst],
                capture_output=True, text=True
            )
            
            if result.returncode == 0:
                print(f"[{self.device_id}] Saved to not-found: {dst}")
            else:
                print(f"[{self.device_id}] not-found backup error: {result.stderr}")

    def clear_and_restart(self):
        """Clear app and prepare for next file"""
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)

    # =========================================================
    # Main Login - After stoplogin, run find-ranger
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
                # Proceed to find ranger process
                self.process_find_ranger(current_filename)
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
                            status = "success"
                            break
                    
                    if back_attempts > 60: break
                    sleep(0.5)
                
                if status == "success":
                    # Run find-ranger instead of stopping
                    status = self.process_find_ranger(current_filename)
                    break
            
            sleep(2)
            if loop_count > 500:
                print(f"[{self.device_id}] Max loops reached.")
                break
        
        return status


if __name__ == "__main__":
    print("=== Auto Find-Ranger Script (Multi-Threaded) ===")
    
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
    
    # Use only the first device (avoid multiple threads on same emulator)
    devices = [devices[0]]
    print(f"[DEV] Using: {devices[0]}")
        
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
    
    # Print config
    ranger_mapping = config.get("ranger_images", {})
    if config.get("custommode") == 1:
        chars_to_show = config.get("custom", {}).get("characters", [])
        mode_str = "Custom Mode (custommode=1)"
    else:
        chars_to_show = config.get("characters", [])
        mode_str = "Find-All Mode"
        
    print(f"\n[CONFIG] {mode_str} - Characters ({len(chars_to_show)}):")
    for c in chars_to_show:
        print(f"  - {c}")
    
    # Show auto-scanned ranger images
    ranger_folder = os.path.join("img", "ranger")
    ranger_files = []
    if os.path.exists(ranger_folder):
        ranger_files = sorted([f for f in os.listdir(ranger_folder) if f.lower().endswith(".png")])
    print(f"[CONFIG] Ranger images in img/ranger/ ({len(ranger_files)}):")
    for f in ranger_files:
        key = f"ranger/{f}"
        if key in ranger_mapping:
            data = ranger_mapping[key]
            print(f"  {f} -> hero: {data.get('hero','?')} | folder: {data.get('folder','?')}")
        else:
            name = os.path.splitext(f)[0]
            print(f"  {f} -> (auto: {name})")
    print()
        
    # Start Threads
    threads = []
    print(f"[INFO] Starting {len(devices)} threads...")
    
    delay = config.get("thread_delay", 5)
    for i, dev in enumerate(devices):
        t = FindRangerBot(dev, file_queue)
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
