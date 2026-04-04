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

# Global Config
config = {
    "first_loop": True,
    "thread_delay": 5
}
adb_path = "adb" # Will be updated by find_adb_executable

def load_config():
    global config
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                config.update(json.load(f))
            print("[OK] Config loaded:", config)
        except Exception as e:
            print(f"[WARN] Error loading config: {e}")
    else:
        try:
            with open("config.json", "w") as f:
                json.dump(config, f, indent=4)
            print("[OK] Created default config.json")
        except:
            pass

def find_adb_executable():
    global adb_path
    
    # 1. Check local adb folder
    if os.path.exists(r"adb\adb.exe"):
        adb_path = os.path.abspath(r"adb\adb.exe")
        print(f"[OK] Found local ADB: {adb_path}")
        
        # Test ADB Execution (Check for missing DLLs)
        try:
            ver = subprocess.check_output([adb_path, "version"], text=True)
            print(f"[DEBUG] {ver.strip()}")
        except Exception as e:
            print(f"[ERR] Failed to execute ADB (Missing DLLs?): {e}")
            return False
            
        return True

    # 2. Check system PATH
    try:
        subprocess.run(["adb", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        adb_path = "adb"
        print("[OK] Found ADB in system PATH")
        return True
    except FileNotFoundError:
        pass

    # 3. Check MuMu specific paths
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
    
    # Specific ports for popular emulators
    manual_ports = [
        62001,  # Nox
        21503,  # MEmu
        7555    # MuMu
    ]
    
    # Standard range - Optimized to odd ports only for speed
    scan_range = [5555 + (i * 2) for i in range(725)] 
    
    all_ports = sorted(list(set(manual_ports + scan_range)))
    
    print(f"[INFO] Scanning {len(all_ports)} ports...")

    def try_connect(port):
        target = f"127.0.0.1:{port}"
        cmd = [adb_path, "connect", target]
        try:
            # print(f"[DEBUG] Scanning {target}...")
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2, text=True)
            output = proc.stdout.strip()
            
            if "connected to" in output:
                 print(f"[OK] Connected to {target}")
            elif "refused" not in output and "cannot connect" not in output:
                 print(f"[DBG] {target} -> {output}")
            # else:
            #      print(f"[FAIL] {target}")
                 
        except subprocess.TimeoutExpired:
            # print(f"[TIMEOUT] {target}")
            pass
        except Exception as e:
            print(f"[ERR] {target}: {e}")

    # Use ThreadPoolExecutor for parallel scanning
    # Force iteration to ensure exceptions are caught/handled if strict=True (though we swallow them)
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        list(executor.map(try_connect, all_ports))
            
    print("[OK] Port scan finished.")

def get_connected_devices():
    try:
        # Use simple os.popen or subprocess to be safer with paths
        # Quote path just in case
        adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
        cmd = f'{adb_cmd} devices'
        # print(f"[DEBUG] Running: {cmd}")
        result = subprocess.check_output(cmd, shell=True, text=True)
        print(f"[DEBUG] Raw 'adb devices' output:\n{result}")
            
        lines = result.strip().split("\n")[1:]
        devices = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                # Only keep 127.0.0.1:* devices, skip emulator-* etc.
                if parts[0].startswith("127.0.0.1:"):
                    devices.append(parts[0])
        return devices
    except Exception as e:
        print(f"[FAIL] Error getting devices: {e}")
        return []

class RangerBot(threading.Thread):
    def __init__(self, device_id, file_queue):
        threading.Thread.__init__(self)
        self.device_id = device_id
        self.file_queue = file_queue
        self.daemon = True
        
        # Determine unique filename for this thread
        safe_dev = device_id.replace(":", "_")
        self.filename = os.path.join(tempfile.gettempdir(), f"screen-{safe_dev}.png")
        self.first_loop_done = not config.get("first_loop", True)
        
        # Sequence Definitions (prefix @ = checkpoint: wait until found but don't click)
        self.seq1 = ['icon.png', 'apple.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png']
        self.seq2 = ['check-gusetid.png', 'check-gusetid1.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png', 'check-ok1.png', 'check-ok2.png', 'check-ok3.png', 'check-ok4.png']
        
        self.adb_cmd = f'"{adb_path}"' if " " in adb_path else adb_path
        self._screen = None  # Cached screen image

    def run(self):
        try:
            print(f"[{self.device_id}] Bot Thread Started", flush=True)
            
            while True:
                # Check queue first to see if we should just exit if empty? 
                # But maybe we need to process first_loop even if queue is empty?
                # Usually we process files. If no files, we stop.
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
                            continue # Retry first loop

                    # 1. Get File
                    try:
                        xml_file = self.file_queue.get(timeout=2)
                    except queue.Empty:
                        break
                    
                    print(f"[{self.device_id}] Processing file: {os.path.basename(xml_file)}")

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
                        
                        # 3. Login
                        status = self.main_login(injected_file)
                        
                        if status == "success":
                            self.handle_success(injected_file)
                        elif status == "failed":
                            self.handle_failure(injected_file)
                            self.first_loop_done = False # Reset flow
                        else:
                            print(f"[{self.device_id}] Unknown status. Moving to next.")
                    else:
                        # Injection failed, maybe try next file or same file? 
                        # For now, it's consumed from queue effectively.
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
        
        print(f"[{self.device_id}] ✅ Login SUCCESS. Moving file.")
        dst = os.path.join(success_path, os.path.basename(file_path))
        try:
            shutil.move(file_path, dst)
        except Exception as e:
            print(f"[{self.device_id}] Error moving file: {e}")

    def handle_failure(self, file_path):
        failed_path = os.path.join(os.getcwd(), "login-failed")
        if not os.path.exists(failed_path): os.makedirs(failed_path)
        
        # 1. Pull the actual shared_pref that failed (from device)
        # Note: The original 'save_failed_file' logic pulled FROM device TO login-failed
        
        src_remote = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        dst_local = os.path.join(failed_path, os.path.basename(file_path))
        
        print(f"[{self.device_id}] 📥 Pulling failed file info...")
        
        # Copy to tmp then pull
        temp_remote = f"/data/local/tmp/failed_pref_{self.device_id.replace(':','_')}.xml"
        self.adb_shell(f"su -c 'cp {src_remote} {temp_remote}'")
        self.adb_shell(f"su -c 'chmod 666 {temp_remote}'")
        self.adb_run([self.adb_cmd, "-s", self.device_id, "pull", temp_remote, dst_local])
        
        print(f"[{self.device_id}] Saved failed file to {dst_local}")
        
        # 2. Delete original from backup (since we moved the result to login-failed)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"[{self.device_id}] 🗑️ Deleted original file from backup.")
        except Exception as e:
            print(f"[{self.device_id}] Error deleting original: {e}")

    # --- Interaction Methods ---
    _template_cache = {}  # Class-level cache for template images
    
    @classmethod
    def _get_template(cls, template_path):
        """Cache template images in RAM to avoid reading from disk every time"""
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
        """Capture screen and load into RAM"""
        try:
            result = subprocess.run(
                [self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
            )
            if result.stdout and len(result.stdout) > 100:
                img = cv2.imdecode(np.frombuffer(result.stdout, np.uint8), 0)
                if img is not None:
                    self._screen = img
                    return
        except Exception:
            pass
        
        # Fallback
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "screencap", "-p", "/sdcard/screen.png"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", "/sdcard/screen.png", self.filename],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if os.path.exists(self.filename):
            self._screen = cv2.imread(self.filename, 0)
        else:
            self._screen = None
    
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
        """Capture + find (use when you need fresh screen)"""
        self.capture_screen()
        return self._find_in_screen(template_path, similarity)
    
    def exists(self, template_path, similarity=0.8):
        return self.find(template_path, similarity) is not None

    def exists_in_cache(self, template_path, similarity=0.8):
        """Check if template exists in already-captured screen (no new capture)"""
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
        """Check error images using cached screen (no new capture)"""
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

    # --- Logic Methods ---
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
        
        # 0. Force Stop App + wait for process to fully die
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)
        
        # 0.5 Kill any remaining processes
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
                # 1. Clean old files first
                self.adb_shell(f"su -c 'rm -f {final} && rm -f {tmp}'")
                
                # 2. Push to temp
                self.adb_run([self.adb_cmd, "-s", self.device_id, "push", src, tmp], timeout=30, check=True)
                
                # 3. Verify push succeeded (check file size on device)
                size_check = self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", f"stat -c %s {tmp}"], text=True)
                remote_size_str = size_check.stdout.strip()
                remote_size = int(remote_size_str) if remote_size_str.isdigit() else 0
                
                if remote_size != src_size:
                    print(f"[{self.device_id}] Size mismatch! Local:{src_size} Remote:{remote_size} (Attempt {attempt})")
                    sleep(1)
                    continue
                
                # 4. Move + Fix permissions
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
                
                # 5. Verify final file exists and has correct size
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
            
            # Restart App
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
            
            # Checkpoint: wait until image appears but don't click
            if isinstance(item, str) and item.startswith('@'):
                checkpoint_img = item[1:]  # remove @ prefix
                print(f"[{self.device_id}] Waiting for checkpoint {checkpoint_img}...")
                while True:
                    self.capture_screen()
                    # Check icon first
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
            # Skip icon.png in sequence if it's the item to find (avoid loop)
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
                
                # 1. Work first!
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

                # 2. Casually check
                self._check_and_click_icon()
                
                # Check for bugs while waiting
                if self.check_error_images():
                    print(f"[{self.device_id}] Bug found during sequence! Restarting first_loop...")
                    return False

                sleep(1)
                start_wait += 1
            
            if not found:
                 print(f"[{self.device_id}] Failed to find {img}. Sequence broken.")
                 return False
                 
        return True

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

            # Capture screen ONCE per loop
            self.capture_screen()

            # Crash/Icon Check (Restart if found)
            if self.exists_in_cache(r"img\icon.png"):
                print(f"[{self.device_id}] Found icon.png (App Closed?). Relaunching...")
                self.click(r"img\icon.png")
                sleep(5)
                continue
            
            # fixalerterror1 Check
            if self.exists_in_cache(r"img\fixalerterror1.png"):
                print(f"[{self.device_id}] Found fixalerterror1.png! Clicking to dismiss...")
                self.click(r"img\fixalerterror1.png")
                sleep(2)
                continue
                
            # Success
            if self.exists_in_cache(r"img\stoplogin.png"):
                print(f"[{self.device_id}] Found stoplogin (Success)")
                status = "success"
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
            
            # fixplay.png Check
            if self.exists_in_cache(r"img\fixplay.png"):
                print(f"[{self.device_id}] Found fixplay.png! Clicking...")
                self.click(r"img\fixplay.png")
                sleep(1)
                continue
            
            # Event
            if self.exists_in_cache(r"img\event.png"):
                print(f"[{self.device_id}] Handling Event...")
                self.click(r"img\event.png")
                sleep(1)
                
                # Back loop
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
                
                if status == "success": break
            
            sleep(2)
            if loop_count > 500:
                print(f"[{self.device_id}] Max loops reached.")
                break
        
        # Cleanup
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        return status


if __name__ == "__main__":
    print("=== Auto ADB Ranger Script (Multi-Threaded) ===")
    
    load_config()
    
    
    if not find_adb_executable():
        print("ADB Not Found.")
        sys.exit(1)
    
    # Reset ADB to fix stale connections
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
        
    # Start Threads
    threads = []
    print(f"[INFO] Starting {len(devices)} threads...")
    
    delay = config.get("thread_delay", 5)
    for i, dev in enumerate(devices):
        t = RangerBot(dev, file_queue)
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