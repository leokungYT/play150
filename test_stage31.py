import cv2
import numpy as np
import subprocess
import time
import os
from threading import Thread

class Quest151Interrupt(Exception):
    pass

class Stage31TestBot:
    def __init__(self, device_id="127.0.0.1:5557"):
        self.device_id = device_id
        self.adb_cmd = "adb"
        self.in_quest_routine = False
        self.getclearquest = 1  # 1 = Quest Routine, 0 = Backup XML
        self.current_xml_name = "account.xml"
        
        # Hide console window for subprocess
        self.kwargs = {}
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            self.kwargs['startupinfo'] = startupinfo
            
        print(f"[{self.device_id}] Initializing Stage 31 Test Bot...")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "echo", "connected"], **self.kwargs)

    def capture_screen(self):
        try:
            result = subprocess.run([self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                                    capture_output=True, check=True, **self.kwargs)
            image_array = np.asarray(bytearray(result.stdout), dtype=np.uint8)
            self.screen_bgr = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
            return True
        except Exception as e:
            print(f"[{self.device_id}] Screen capture error: {e}")
            self.screen_bgr = None
            return False

    def tap(self, x, y, label=""):
        if label:
            print(f"[{self.device_id}] Tapping {label} at ({x}, {y})")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "tap", str(x), str(y)], **self.kwargs)

    def find_image(self, template_path, threshold=0.8):
        if self.screen_bgr is None:
            return None
        if not os.path.exists(template_path):
            return None
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template is None:
            return None
        res = cv2.matchTemplate(self.screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= threshold)
        if len(loc[0]) > 0:
            return (int(loc[1][0] + template.shape[1]/2), int(loc[0][0] + template.shape[0]/2))
        return None

    def wait_and_click(self, template_path, timeout=10, threshold=0.8):
        print(f"[{self.device_id}]   ...searching for {os.path.basename(template_path)} (timeout {timeout}s)")
        start_time = time.time()
        while time.time() - start_time < timeout:
            if not self.capture_screen():
                time.sleep(0.5)
                continue
            
            if not self.in_quest_routine:
                if self.find_image("img/stage151.png", threshold=0.95):
                    raise Quest151Interrupt()

            pos = self.find_image(template_path, threshold)
            if pos:
                print(f"[{self.device_id}] Clicking {os.path.basename(template_path)}...")
                self.tap(pos[0], pos[1], label=os.path.basename(template_path))
                return pos
            time.sleep(0.5)
        return None

    def process_sequence(self, sequence_def):
        for item in sequence_def:
            img_path = item["img"]
            img_name = os.path.basename(img_path)
            is_loop = item.get("loop", True)
            to = item.get("timeout", None)

            print(f"[{self.device_id}]   SeqWait for {img_name}... (Timeout: {to or 'Infinite'})")
            start_wait = time.time()
            found = False
            while True:
                if to is not None and time.time() - start_wait > to:
                    break
                self.capture_screen()
                
                if not self.in_quest_routine:
                    if self.find_image("img/stage151.png", threshold=0.95):
                        raise Quest151Interrupt()

                if self.find_image(img_path, 0.8):
                    found = True
                    break
                time.sleep(0.5)
            
            if not found:
                print(f"[{self.device_id}]     {img_name} not found, skipping.")
                continue
            
            print(f"[{self.device_id}]     Found {img_name}! Executing '{'loop-click' if is_loop else 'single-click'}'...")
            while True:
                self.capture_screen()
                p = self.find_image(img_path, 0.8)
                if p:
                    self.tap(p[0], p[1], label=img_name)
                    time.sleep(0.5)
                    if not is_loop:
                        break
                else:
                    break

    def handle_battle(self):
        print(f"[{self.device_id}] >>> Phase 2: Battle 31+ System...")
        
        # 1. กดรวม 6 จุด
        hero_coords = [(278, 521), (384, 514), (483, 508), (582, 515), (683, 517), (146, 483)]
        for _ in range(6):
            tap_chain = " & ".join([f"input tap {c[0]} {c[1]}" for c in hero_coords]) + " & wait"
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", tap_chain], **self.kwargs)
            time.sleep(0.1)

        # 2. กดตำแหน่งเสริม 7 
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
            
            if not self.in_quest_routine:
                if self.find_image("img/stage151.png", threshold=0.95):
                    stop_spam = True
                    spam_thread.join()
                    raise Quest151Interrupt()

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

    def handle_finish(self):
        print(f"[{self.device_id}] >>> Phase 3: Finish & Rewards (Priority Loop)...")
        
        start_time = time.time()
        while time.time() - start_time < 60:
            self.capture_screen()
            
            if not self.in_quest_routine:
                if self.find_image("img/stage151.png", threshold=0.95):
                    raise Quest151Interrupt()

            pos_ns = self.find_image("img/nextstage.png", threshold=0.8)
            if pos_ns:
                time.sleep(0.5)
                self.tap(pos_ns[0], pos_ns[1], label="nextstage_repeat")
                print(f"[{self.device_id}]   !!! FLOATING NEXTSTAGE DETECTED !!! Tapping 218, 46 x6...")
                for _ in range(6):
                    time.sleep(2.0)
                    self.tap(218, 46)
                print(f"[{self.device_id}]   Bypassing the rest of the clear sequence.")
                return "jump"

            pos_nn = self.find_image("img/nextnew.png", threshold=0.8)
            if pos_nn:
                print(f"[{self.device_id}]   !!! FLOATING NEXTNEW DETECTED !!! Tapping 162,32 -> Jump.")
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

    def handle_quest_151(self):
        self.in_quest_routine = True
        try:
            print(f"[{self.device_id}] >>> STAGE 151 QUEST ROUTINE STARTED <<<")
            
            # 1. Initial setup
            self.wait_and_click("img/back-stage.png", timeout=10)
            self.wait_and_click("img/quest-stage1.png", timeout=99999)
            self.wait_and_click("img/getquse1.png", timeout=99999)
            self.wait_and_click("img/okquest.png", timeout=99999)

            # 2. #playnumber1 quest
            print(f"[{self.device_id}]   Processing Play 1 Quest...")
            self.wait_and_click("img/goquse1.png", timeout=99999)
            pos_pq1 = self.wait_and_click("img/playques1.png", timeout=99999)
            if pos_pq1:
                # Tap original position 1 time
                self.tap(pos_pq1[0], pos_pq1[1], label="playques1_rep")
            
            print(f"[{self.device_id}]   Delaying 3 seconds...")
            time.sleep(3)
            
            print(f"[{self.device_id}]   Tapping coordinate 125, 29...")
            self.tap(125, 29, label="coord_125_29")
            time.sleep(1)
            print(f"[{self.device_id}]   Tapping coordinate 393, 449...")
            self.tap(393, 449, label="coord_393_449")
            time.sleep(1)

            for q_img in ["img/playquest2.png", "img/playquest3.png", "img/playquest4.png"]:
                self.wait_and_click(q_img, timeout=99999)

            print(f"[{self.device_id}]   Executing quest swipe...")
            subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", "99", "447", "395", "259", "800"], **self.kwargs)
            time.sleep(1)

            self.wait_and_click("img/playquest5.png", timeout=99999)
            self.wait_and_click("img/playquest6.png", timeout=99999)

            # 3. #play2 quest
            print(f"[{self.device_id}]   Processing Play 2 Quest...")
            print(f"[{self.device_id}]   Tapping 282, 196 twice...")
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
        finally:
            self.in_quest_routine = False

    def handle_stage151_backup(self):
        """Standard backup logic for Stage 151."""
        print(f"[{self.device_id}] Stage 151 detected. Mode: Backup XML.")
        backup_dir = "backup-id"
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        source_remote = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        dest_local = os.path.join(backup_dir, f"stage151_{self.current_xml_name}")
        subprocess.run([self.adb_cmd, "-s", self.device_id, "shell", "su -c 'chmod 666 " + source_remote + "'"], **self.kwargs)
        subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", source_remote, dest_local], **self.kwargs)
        print(f"[{self.device_id}] Backup successful: {dest_local}")

    def run_stage31_test(self):
        print(f"[{self.device_id}] === STARTING TEST LOOP (STAGE 31+) ===")
        is_first_run_of_stage31 = True
        start_from_side = False
        
        while True:
            try:
                self.capture_screen()
                if self.find_image("img/stage151.png", threshold=0.95):
                    if self.getclearquest == 1:
                        print(f"[{self.device_id}] !!! STAGE 151 DETECTED !!! Triggering Quest Routine...")
                        self.handle_quest_151()
                    else:
                        print(f"[{self.device_id}] !!! STAGE 151 DETECTED !!! Backup Mode (XML)...")
                        self.handle_stage151_backup()
                        break
                    continue

                print(f"[{self.device_id}] >>> Phase 1: Navigating (Dictionary Loop Mode)...")
                
                if start_from_side or is_first_run_of_stage31:
                    print(f"[{self.device_id}]   [Jump Recover] Checking nextstage -> nextnew before side...")
                    self.process_sequence([
                        {"img": "img/nextstage.png", "loop": True, "timeout": 10},
                        {"img": "img/nextnew.png", "loop": True, "timeout": 10}
                    ])
                    start_from_side = False
                else:
                    while True:
                        print(f"[{self.device_id}]   [Pre-Nav Loop] Checking clearstop -> nextstage -> nextnew...")
                        self.process_sequence([
                            {"img": "img/clearstop.png", "loop": True, "timeout": 10},
                            {"img": "img/nextstage.png", "loop": True, "timeout": 10},
                            {"img": "img/nextnew.png", "loop": True, "timeout": 10}
                        ])
                        
                        self.capture_screen()
                        if self.find_image("img/side.png", 0.8) or self.find_image("img/buyhelp.png", 0.8) or self.find_image("img/startnew.png", 0.8):
                            print(f"[{self.device_id}]   Map context verified. Proceeding to main Navigation Sequence...")
                            break
                            
                        print(f"[{self.device_id}]   Not on Map yet! Looping back to clearstop -> nextstage -> nextnew (Timeout 10s)...")
                
                nav_seq = [
                    {"img": "img/side.png", "loop": True, "timeout": None},
                    {"img": "img/buyhelp.png", "loop": False, "timeout": None},
                    {"img": "img/startnew.png", "loop": True, "timeout": None}
                ]
                self.process_sequence(nav_seq)

                self.handle_battle()
                res = self.handle_finish()

                if is_first_run_of_stage31:
                    print(f"[{self.device_id}]   STAGE 31 SPECIAL AFTER CLEAR (som -> nextstage -> autoadvance1 -> 751,505x8)")
                    self.wait_and_click("img/som.png", timeout=15)
                    pos_ns = self.wait_and_click("img/nextstage.png", timeout=10)
                    if pos_ns:
                        time.sleep(0.5)
                        self.tap(pos_ns[0], pos_ns[1], label="nextstage_repeat")
                    self.wait_and_click("img/autoadvance.png", timeout=10)
                    for _ in range(8):
                        self.tap(751, 505, label="special_pos_751_505")
                        time.sleep(0.3)
                    
                    is_first_run_of_stage31 = False

                if res == "jump":
                    print(f"[{self.device_id}]   JUMP executed! Returning directly to Side...")
                    start_from_side = True
            except Quest151Interrupt:
                print(f"[{self.device_id}] [QuestInterrupt] Breaking current phase to handle Stage 151...")
                self.handle_quest_151()
                continue

if __name__ == "__main__":
    bot = Stage31TestBot("127.0.0.1:5557")
    bot.run_stage31_test()
