from ppadb.client import Client as AdbClient
import cv2
import numpy as np

import time
from threading import Thread, Lock, Semaphore
import os
import subprocess
from queue import Queue
import gc
import psutil
import concurrent.futures
import socket
import re
import shutil
from typing import List
import getpass
from datetime import datetime
import colorama
from colorama import Fore, Style
import sys
colorama.init(autoreset=True)
import json
import threading

# 🚨 MSS/win32gui ปิดแล้ว - กิน memory เยอะทำให้ MuMu ค้าง

# ⭐ Force print to flush immediately (no buffering)
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

# ============================================
# GUI MODULE - ปิดแล้ว! ใช้ CMD แทน เพื่อลด memory/CPU
# ============================================
GUI_ENABLED = False

def start_mini_gui():
    print(f"{Fore.YELLOW}[GUI] GUI ปิดแล้ว - ใช้ CMD แทนเพื่อลด memory{Style.RESET_ALL}")
    return None

# ⭐ AUTO SCAN CONFIGURATION - สแกนหา port อัตโนมัติ
# เริ่มจาก 5557 และเพิ่มทีละ 2 (5557, 5559, 5561, 5563...)
# สำหรับ 30 จอ
START_PORT = 5557
MAX_DEVICES = 30
MUMU_PORTS = [START_PORT + (i * 2) for i in range(MAX_DEVICES)]  # [5557, 5559, 5561, ..., 5615]

# ----- Simplified UI Stats Class -----
class SimpleUIStats:
    def __init__(self):
        self.total_files = 0
        self.successful_logins = 0
        self.failed_logins = 0
        self.processed_files = 0
        self.connected_devices = 0
        self.lock = Lock()
        self.last_update = time.time()
        self.update_interval = 30
        self.start_time = datetime.now().strftime('%H:%M:%S')
        
    def should_update(self):
        return time.time() - self.last_update >= self.update_interval
        
    def force_update(self):
        self.last_update = 0
        
    def update(self, total=None, processed=None, success=None, fail=None, devices=None):
        with self.lock:
            if total is not None: self.total_files = total
            if processed is not None: self.processed_files = processed
            if success is not None: self.successful_logins = success
            if fail is not None: self.failed_logins = fail
            if devices is not None: self.connected_devices = devices
            
            if self.should_update():
                self.draw()
                self.last_update = time.time()

    def get_progress_percent(self):
        if self.total_files == 0: 
            return 0
        return int(100 * self.processed_files / self.total_files)

    def draw(self):
        # ⭐ ปิด auto clear เพื่อให้เห็น log จาก threads
        # แสดง status line แทนการ clear ทุกครั้ง
        status_line = f"[STATUS] Files: {self.total_files:,} | Completed: {self.processed_files:,} | Failed: {self.failed_logins:,}"
        print(f"{Fore.CYAN}{status_line}{Style.RESET_ALL}", flush=True)
    
    def _create_progress_bar(self, percent, length=30):
        filled = int(length * percent / 100)
        if percent >= 80:
            color = Fore.GREEN
        elif percent >= 40:
            color = Fore.YELLOW
        else:
            color = Fore.RED
        
        bar = f"{color}{'█' * filled}{Fore.WHITE}{'░' * (length - filled)}{Style.RESET_ALL}"
        return bar

    def print_simple_message(self, message):
        current_time = datetime.now().strftime('%H:%M:%S')
        print(f"{Fore.WHITE}[{current_time}] {message}{Style.RESET_ALL}")

ui_stats = SimpleUIStats()
adb_push_semaphore = Semaphore(2) # Limit to 2 for stability
screencap_semaphore = Semaphore(3)  # ⭐ จำกัด screencap พร้อมกันสูงสุด 3 ตัว ลดโหลด MuMu

class DeviceState:
    def __init__(self):
        self.lock = Lock()
        self.devices_status = {}
        self.file_queue = Queue()
        self.processed_files = set()
        self.original_filenames = {}
        self.success_count = 0
        self.fail_count = 0
        self.processing_count = 0
        self.device_first_loop = {}
        self.image_cache = {}

device_state = DeviceState()

# ⭐ ADB Standard Capture - เสถียร ไม่ทำให้ MuMu ค้าง + Throttle
def get_screen_capture(device):
    """ADB Standard Screencap - มี throttle ป้องกันโหลด MuMu"""
    try:
        with screencap_semaphore:  # ⭐ จำกัดไม่ให้ดึง screencap พร้อมกันเยอะเกิน
            cap = device.screencap()
            if cap:
                image = np.frombuffer(cap, dtype=np.uint8)
                return cv2.imdecode(image, cv2.IMREAD_GRAYSCALE)
        return None
    except Exception:
        return None

def load_template(find_img_path):
    # ขีดจำกัดขนาด cache (เก็บได้สูงสุด 50 รูป)
    MAX_CACHE_SIZE = 50
    if len(device_state.image_cache) >= MAX_CACHE_SIZE:
        oldest_key = next(iter(device_state.image_cache))
        del device_state.image_cache[oldest_key]
    
    if find_img_path not in device_state.image_cache:
        # Load as grayscale for performance
        template = cv2.imread(find_img_path, cv2.IMREAD_GRAYSCALE)
        if template is not None:
            device_state.image_cache[find_img_path] = template
    return device_state.image_cache.get(find_img_path)

def ImgSearchADB(adb_img, find_img_path, threshold=0.95, method=cv2.TM_CCOEFF_NORMED):
    try:
        # Convert input to grayscale if it is color
        if len(adb_img.shape) == 3:
            img_gray = cv2.cvtColor(adb_img, cv2.COLOR_BGR2GRAY)
        else:
            img_gray = adb_img

        find_img = load_template(find_img_path)
        if find_img is None:
            return []
            
        needle_w = find_img.shape[1]
        needle_h = find_img.shape[0]
        
        result = cv2.matchTemplate(img_gray, find_img, method)
        locations = np.where(result >= threshold)
        locations = list(zip(*locations[::-1]))
        result = None
        
        rectangles = []
        for loc in locations:
            rect = [int(loc[0]), int(loc[1]), needle_w, needle_h]
            rectangles.append(rect)
            rectangles.append(rect)
            
        if len(rectangles) > 0:
            rectangles, _ = cv2.groupRectangles(rectangles, groupThreshold=1, eps=1)
            
        points = []
        if len(rectangles):
            for (x, y, w, h) in rectangles:
                center_x = x + int(w/2)
                center_y = y + int(h/2)
                points.append((center_x, center_y))
                
        return points
        
    except Exception:
        return []

def clean_memory():
    """⚡ TURBO: Aggressive memory cleanup เพื่อป้องกัน slow down"""
    try:
        # Clear image cache ถ้าเกิน 30 รูป
        if len(device_state.image_cache) > 30:
            # เก็บแค่ 10 รูปล่าสุด
            keys = list(device_state.image_cache.keys())
            for key in keys[:-10]:
                del device_state.image_cache[key]
        
        # Force garbage collection
        gc.collect()
    except Exception:
        pass

def get_resource_usage():
    process = psutil.Process()
    cpu_percent = process.cpu_percent()
    memory_info = process.memory_info()
    return cpu_percent, memory_info.rss / 1024 / 1024

def check_stopcheck_with_multiple_thresholds(adb_img):
    try:
        thresholds = [0.95, 0.9, 0.85, 0.8]
        for threshold in thresholds:
            stopcheck_pos = ImgSearchADB(adb_img, 'img/stopcheck.png', threshold=threshold)
            if stopcheck_pos:
                return stopcheck_pos
        return []
    except Exception:
        return []

def count_xml_files(directory):
    try:
        total_count = 0
        for root, dirs, files in os.walk(directory):
            total_count += len([f for f in files if f.endswith('.xml')])
        return total_count
    except Exception as e:
        print(f"{Fore.RED}Error counting XML files: {str(e)}{Style.RESET_ALL}")
        return 0

def get_backup_folder():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    backup_path = os.path.join(current_dir, "backup", "backupxml")
    if not os.path.exists(backup_path):
        try: 
            os.makedirs(backup_path)
        except: 
            pass
    return backup_path

def get_return_folder():
    """สร้างโฟลเดอร์ return สำหรับเก็บไฟล์ที่ process แล้ว"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return_path = os.path.join(current_dir, "return")
    if not os.path.exists(return_path):
        try: 
            os.makedirs(return_path)
            print(f"{Fore.GREEN}[INFO] Created return folder: {return_path}{Style.RESET_ALL}")
        except: 
            pass
    return return_path

def move_to_return(device):
    """ย้ายไฟล์ต้นฉบับไปโฟลเดอร์ return หลังจาก process เสร็จ"""
    try:
        original_filename = device_state.original_filenames.get(device.serial)
        if not original_filename:
            return False
            
        return_folder = get_return_folder()
        source_path = os.path.join(source_folder, original_filename)
        
        # ถ้าไฟล์ต้นฉบับยังอยู่ใน source folder ให้ย้ายไป return
        if os.path.exists(source_path):
            dest_path = os.path.join(return_folder, original_filename)
            try:
                shutil.move(source_path, dest_path)
                print(f"{Fore.CYAN}[DEVICE {device.serial}] Moved processed file to return: {original_filename}{Style.RESET_ALL}")
                return True
            except Exception as e:
                print(f"{Fore.YELLOW}[DEVICE {device.serial}] Could not move to return: {str(e)}{Style.RESET_ALL}")
                return False
        else:
            # ไฟล์ถูกลบแล้ว ไม่ต้องทำอะไร
            return True
    except Exception as e:
        print(f"{Fore.RED}[DEVICE {device.serial}] Error in move_to_return: {str(e)}{Style.RESET_ALL}")
        return False

source_folder = get_backup_folder()

def has_xml_files():
    try:
        # ⭐ FIX: กรองเฉพาะไฟล์ต้นฉบับ ไม่นับไฟล์ temp
        xml_files = [f for f in os.listdir(source_folder) 
                     if f.endswith('.xml') and not f.startswith('_LINE_COCOS_PREF_KEY_')]
        return len(xml_files) > 0
    except FileNotFoundError:
        return False

def backup_to_login_send(device):
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        login_send_dir = os.path.join(current_dir, "login-send")
        if not os.path.exists(login_send_dir):
            os.makedirs(login_send_dir)
            
        source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        original_filename = device_state.original_filenames.get(device.serial)
        
        if not original_filename:
            return False
            
        backup_path = os.path.join(login_send_dir, original_filename)
        device.shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
        device.shell(f"su -c 'chmod 777 {source_path}'")
        
        max_retries = 2
        for attempt in range(max_retries):
            try:
                pull_command = f'adb -s {device.serial} pull "{source_path}" "{backup_path}"'
                result = subprocess.run(
                    pull_command, 
                    shell=True, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15
                )
                
                if result.returncode == 0 and os.path.exists(backup_path):
                    return True
                else:
                    if attempt < max_retries - 1:
                        time.sleep(0.1)
                    else:
                        return False
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(0.1)
                else:
                    return False
    except Exception:
        return False

def save_fail(device):
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        not_found_dir = os.path.join(current_dir, "login-fail")
        if not os.path.exists(not_found_dir):
            os.makedirs(not_found_dir)
            
        source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        original_filename = device_state.original_filenames.get(device.serial)
        
        if not original_filename:
            return False
            
        backup_path = os.path.join(not_found_dir, original_filename)
        
        max_retries = 2
        for attempt in range(max_retries):
            try:
                pull_command = f'adb -s {device.serial} pull "{source_path}" "{backup_path}"'
                result = subprocess.run(
                    pull_command, 
                    shell=True, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15
                )
                
                if result.returncode == 0 and os.path.exists(backup_path):
                    ui_stats.update(fail=ui_stats.failed_logins + 1)
                    return True
                else:
                    if attempt < max_retries - 1:
                        time.sleep(0.1)
                    else:
                        return False
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(0.1)
                else:
                    return False
    except Exception:
        return False

def clear_game_data(device):
    try:
        device.shell("pm clear com.linecorp.LGRGS")
        time.sleep(2)
        return True
    except Exception:
        return False

def clear_app(device):
    try:
        device.shell("am force-stop com.google.android.googlequicksearchbox")
        device.shell("am force-stop com.android.browser")
        device.shell("am force-stop com.linecorp.LGRGS")
        time.sleep(0.2)
    except Exception as e:
        pass



def first_loop_process(device):
    """ขั้นตอนการทำงาน loop แรก พร้อมตรวจสอบ fixcak.png"""
    try:
        print(f"{Fore.CYAN}[DEVICE {device.serial}] Starting first loop process{Style.RESET_ALL}")
        
        # ลบข้อมูลเกมก่อน
        clear_game_data(device)
        time.sleep(3)
        
        # เปิดแอป
        device.shell("am force-stop com.linecorp.LGRGS")
        time.sleep(1)
        device.shell("am start -n com.linecorp.LGRGS/com.linecorp.common.activity.LineActivity")
        time.sleep(10)
        
        # ตรวจหา test.png
        test_found = False
        test_timeout = 120  # เพิ่มเวลา timeout
        test_start_time = time.time()
        
        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Looking for test.png (timeout: {test_timeout}s)...{Style.RESET_ALL}")
        
        while time.time() - test_start_time < test_timeout:
            try:
                elapsed_time = time.time() - test_start_time
                
                cap = device.screencap()
                image = np.frombuffer(cap, dtype=np.uint8)
                adb_img = cv2.imdecode(image, cv2.IMREAD_COLOR)
                
                # *** ตรวจสอบ fixcak.png ก่อนเสมอ ***
                fixcak_pos = ImgSearchADB(adb_img, 'img/fixcak.png')
                if fixcak_pos:
                    print(f"{Fore.RED}[DEVICE {device.serial}] Found fixcak.png in first_loop!{Style.RESET_ALL}")
                    print(f"{Fore.RED}[DEVICE {device.serial}] Clearing app and restarting first_loop...{Style.RESET_ALL}")
                    clear_app(device)
                    time.sleep(6)
                    return "restart_first_loop"
                
                if check_black_screen(adb_img, threshold=0.8):
                    if black_screen_timer is None:
                        black_screen_timer = time.time()
                        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Black screen detected, starting timer...{Style.RESET_ALL}")
                    else:
                        elapsed = time.time() - black_screen_timer
                        if elapsed >= 8:
                            print(f"{Fore.RED}[DEVICE {device.serial}] Black screen > 8s, clearing app and restarting...{Style.RESET_ALL}")
                            clear_app(device)
                            time.sleep(3)
                            device.shell("am start -n com.linecorp.LGRGS/com.linecorp.common.activity.LineActivity")
                            time.sleep(2)
                            black_screen_timer = None
                            continue
                else:
                    black_screen_timer = None
                
                # ตรวจสอบ stopcheck.png
                thresholds = [0.95, 0.9, 0.85, 0.8]
                for threshold in thresholds:
                    stopcheck_pos = ImgSearchADB(adb_img, 'img/stopcheck.png', threshold=threshold)
                    if stopcheck_pos:
                        print(f"{Fore.RED}[DEVICE {device.serial}] Found stopcheck.png!{Style.RESET_ALL}")
                        clear_app(device)
                        time.sleep(2)
                        return "complete"
                
                test_pos = ImgSearchADB(adb_img, 'img/test.png')
                if test_pos:
                    print(f"{Fore.GREEN}[DEVICE {device.serial}] Found test.png!{Style.RESET_ALL}")
                    device.shell(f"input tap {test_pos[0][0]} {test_pos[0][1]}")
                    test_found = True
                    break
                
                if int(elapsed_time) % 10 == 0:
                    print(f"{Fore.YELLOW}[DEVICE {device.serial}] Still searching for test.png...({int(elapsed_time)}s elapsed){Style.RESET_ALL}")
                
                time.sleep(1.0)  # ⭐ PERF: ลดจาก 0.5 เป็น 1.0 ลดโหลด MuMu
                
            except Exception as e:
                print(f"{Fore.RED}[DEVICE {device.serial}] Error: {str(e)}{Style.RESET_ALL}")
                continue
        
        if not test_found:
            print(f"{Fore.RED}[DEVICE {device.serial}] test.png not found, restarting{Style.RESET_ALL}")
            return False
        
        # ตรวจสอบ closeapp.png หลังจากกด test.png
        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Checking for closeapp.png (10 seconds)...{Style.RESET_ALL}")
        closeapp_timeout = 10
        closeapp_start_time = time.time()
        closeapp_found = False
        
        while time.time() - closeapp_start_time < closeapp_timeout:
            try:
                cap = device.screencap()
                image = np.frombuffer(cap, dtype=np.uint8)
                adb_img = cv2.imdecode(image, cv2.IMREAD_COLOR)
                
                # *** ตรวจสอบ fixcak.png ***
                fixcak_pos = ImgSearchADB(adb_img, 'img/fixcak.png')
                if fixcak_pos:
                    print(f"{Fore.RED}[DEVICE {device.serial}] Found fixcak.png!{Style.RESET_ALL}")
                    clear_app(device)
                    time.sleep(6)
                    return "restart_first_loop"
                
                closeapp_pos = ImgSearchADB(adb_img, 'img/closeapp.png')
                if closeapp_pos:
                    print(f"{Fore.RED}[DEVICE {device.serial}] Found closeapp.png! Clearing app and restarting first_loop...{Style.RESET_ALL}")
                    clear_app(device)
                    time.sleep(2)
                    closeapp_found = True
                    return "restart_first_loop"
                
                time.sleep(0.8)  # ⭐ PERF: ลดจาก 0.3 เป็น 0.8 ลดโหลด MuMu
                
            except Exception as e:
                print(f"{Fore.RED}[DEVICE {device.serial}] Error checking closeapp.png: {str(e)}{Style.RESET_ALL}")
                continue
        
        if not closeapp_found:
            print(f"{Fore.GREEN}[DEVICE {device.serial}] closeapp.png not found within 10 seconds, continuing...{Style.RESET_ALL}")
        
        # ตรวจหา save.png
        save_found = False
        save_timeout = 20
        save_start_time = time.time()
        
        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Looking for save.png...{Style.RESET_ALL}")
        
        while time.time() - save_start_time < save_timeout:
            try:
                cap = device.screencap()
                image = np.frombuffer(cap, dtype=np.uint8)
                adb_img = cv2.imdecode(image, cv2.IMREAD_COLOR)
                
                # *** ตรวจสอบ fixcak.png ***
                fixcak_pos = ImgSearchADB(adb_img, 'img/fixcak.png')
                if fixcak_pos:
                    print(f"{Fore.RED}[DEVICE {device.serial}] Found fixcak.png!{Style.RESET_ALL}")
                    clear_app(device)
                    time.sleep(6)
                    return "restart_first_loop"
                
                # ตรวจสอบ stopcheck.png
                thresholds = [0.95, 0.9, 0.85, 0.8]
                for threshold in thresholds:
                    stopcheck_pos = ImgSearchADB(adb_img, 'img/stopcheck.png', threshold=threshold)
                    if stopcheck_pos:
                        print(f"{Fore.RED}[DEVICE {device.serial}] Found stopcheck.png!{Style.RESET_ALL}")
                        clear_app(device)
                        time.sleep(2)
                        return "complete"
                
                save_pos = ImgSearchADB(adb_img, 'img/save.png')
                if save_pos:
                    device.shell(f"input tap {save_pos[0][0]} {save_pos[0][1]}")
                    save_found = True
                    print(f"{Fore.GREEN}[DEVICE {device.serial}] Found save.png!{Style.RESET_ALL}")
                    break
                    
                time.sleep(0.8)  # ⭐ PERF: ลดจาก 0.3 เป็น 0.8 ลดโหลด MuMu
                
            except Exception as e:
                print(f"{Fore.RED}[DEVICE {device.serial}] Error: {str(e)}{Style.RESET_ALL}")
                continue
        
        if not save_found:
            print(f"{Fore.RED}[DEVICE {device.serial}] save.png not found, clearing app{Style.RESET_ALL}")
            clear_app(device)
            time.sleep(2)
            return "restart_from_test"
        
        # ลำดับการตรวจสอบรูปภาพ
        sequence1 = [
            'apple.png', 'check-l1.png', 'check-l2.png', 
            'check-l3.png', 'check-l4.png'
        ]
        
        sequence2 = [
            'check-gusetid.png', 'check-gusetid1.png',
            'check-l1.png', 'check-l2.png', 'check-l3.png', 'check-l4.png',
            'check-ok1.png', 'check-ok2.png', 'check-ok3.png', 'check-ok4.png'
        ]
        
        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Processing sequence 1...{Style.RESET_ALL}")
        
        # ทำงานตาม sequence1 - ไม่ข้ามรูปไหน แค่นับเวลา
        for i, img_name in enumerate(sequence1):
            print(f"{Fore.CYAN}[DEVICE {device.serial}] Looking for {img_name} ({i+1}/{len(sequence1)}){Style.RESET_ALL}")
            found = False
            timeout = 60  # เพิ่มเวลา timeout
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                try:
                    elapsed_time = time.time() - start_time
                    cap = device.screencap()
                    image = np.frombuffer(cap, dtype=np.uint8)
                    adb_img = cv2.imdecode(image, cv2.IMREAD_COLOR)
                    
                    # *** ตรวจสอบ fixcak.png ***
                    fixcak_pos = ImgSearchADB(adb_img, 'img/fixcak.png')
                    if fixcak_pos:
                        print(f"{Fore.RED}[DEVICE {device.serial}] Found fixcak.png in sequence1!{Style.RESET_ALL}")
                        clear_app(device)
                        time.sleep(6)
                        return "restart_first_loop"
                    
                    # ตรวจสอบ stopcheck.png
                    for threshold in thresholds:
                        stopcheck_pos = ImgSearchADB(adb_img, 'img/stopcheck.png', threshold=threshold)
                        if stopcheck_pos:
                            print(f"{Fore.RED}[DEVICE {device.serial}] Found stopcheck.png!{Style.RESET_ALL}")
                            clear_app(device)
                            time.sleep(2)
                            return "complete"
                    
                    pos = ImgSearchADB(adb_img, f'img/{img_name}')
                    if pos:
                        device.shell(f"input tap {pos[0][0]} {pos[0][1]}")
                        found = True
                        print(f"{Fore.GREEN}[DEVICE {device.serial}] Found {img_name}!{Style.RESET_ALL}")
                        
                        if img_name == 'check-l4.png':
                           print(f"{Fore.YELLOW}[DEVICE {device.serial}] Found check-l4.png, waiting 2s...{Style.RESET_ALL}")
                           time.sleep(2)

                        time.sleep(1)
                        break
                    
                    if int(elapsed_time) % 10 == 0:
                        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Searching for {img_name}...({int(elapsed_time)}s elapsed){Style.RESET_ALL}")
                        
                    time.sleep(0.8)  # ⭐ PERF: ลดจาก 0.3 เป็น 0.8 ลดโหลด MuMu
                except Exception:
                    continue
            
            if not found:
                print(f"{Fore.YELLOW}[DEVICE {device.serial}] {img_name} not found, continuing...{Style.RESET_ALL}")
                continue
        
        print(f"{Fore.CYAN}[DEVICE {device.serial}] Sequence 1 completed, waiting 8s then pressing BACK...{Style.RESET_ALL}")
        time.sleep(8)
        device.shell("input keyevent 4")
        time.sleep(2)

        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Processing sequence 2...{Style.RESET_ALL}")
        
        # ทำงานตาม sequence2 - ไม่ข้ามรูปไหน แค่นับเวลา
        for i, img_name in enumerate(sequence2):
            print(f"{Fore.CYAN}[DEVICE {device.serial}] Looking for {img_name} ({i+1}/{len(sequence2)}){Style.RESET_ALL}")
            found = False
            timeout = 60  # เพิ่มเวลา timeout
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                try:
                    elapsed_time = time.time() - start_time
                    cap = device.screencap()
                    image = np.frombuffer(cap, dtype=np.uint8)
                    adb_img = cv2.imdecode(image, cv2.IMREAD_COLOR)
                    
                    # *** ตรวจสอบ fixcak.png ***
                    fixcak_pos = ImgSearchADB(adb_img, 'img/fixcak.png')
                    if fixcak_pos:
                        print(f"{Fore.RED}[DEVICE {device.serial}] Found fixcak.png in sequence2!{Style.RESET_ALL}")
                        clear_app(device)
                        time.sleep(6)
                        return "restart_first_loop"
                    
                    # ตรวจสอบ stopcheck.png
                    for threshold in thresholds:
                        stopcheck_pos = ImgSearchADB(adb_img, 'img/stopcheck.png', threshold=threshold)
                        if stopcheck_pos:
                            print(f"{Fore.RED}[DEVICE {device.serial}] Found stopcheck.png!{Style.RESET_ALL}")
                            clear_app(device)
                            time.sleep(2)
                            return "complete"
                    
                    pos = ImgSearchADB(adb_img, f'img/{img_name}')
                    if pos:
                        device.shell(f"input tap {pos[0][0]} {pos[0][1]}")
                        found = True
                        print(f"{Fore.GREEN}[DEVICE {device.serial}] Found {img_name}!{Style.RESET_ALL}")
                        time.sleep(1)
                        break
                    
                    if int(elapsed_time) % 10 == 0:
                        print(f"{Fore.YELLOW}[DEVICE {device.serial}] Searching for {img_name}...({int(elapsed_time)}s elapsed){Style.RESET_ALL}")
                        
                    time.sleep(0.8)  # ⭐ PERF: ลดจาก 0.3 เป็น 0.8 ลดโหลด MuMu
                except Exception:
                    continue
            
            if not found:
                print(f"{Fore.YELLOW}[DEVICE {device.serial}] {img_name} not found, continuing...{Style.RESET_ALL}")
                continue

        print(f"{Fore.GREEN}[DEVICE {device.serial}] First loop process completed!{Style.RESET_ALL}")
        clear_app(device)
        time.sleep(2)
        print(f"{Fore.GREEN}[DEVICE {device.serial}] App cleared after completion{Style.RESET_ALL}")
        return "complete"
        
    except Exception as e:
        print(f"{Fore.RED}[DEVICE {device.serial}] Error in first loop: {str(e)}{Style.RESET_ALL}")
        clear_app(device)
        time.sleep(2)
        return False



def check_black_screen(adb_img, threshold=0.8):
    try:
        if adb_img is None:
            return False
        
        # Handle both color and grayscale inputs
        if len(adb_img.shape) == 3:
            gray = cv2.cvtColor(adb_img, cv2.COLOR_BGR2GRAY)
        else:
            gray = adb_img
            
        mean_brightness = np.mean(gray)
        is_black = mean_brightness < (255 * threshold / 100)
        
        return is_black
    except Exception:
        return False


def handle_apple_sequence(device):
    """
    ⭐ จัดการขั้นตอนหลังเจอ apple.png
    ทำ sequence1 และ sequence2 ก่อน แล้วค่อย clear_app
    """
    print(f"{Fore.YELLOW}[DEVICE {device.serial}] Starting Apple Sequence...{Style.RESET_ALL}")
    
    # ลำดับการตรวจสอบรูปภาพ (เหมือน first_loop เป๊ะๆ)
    sequence1 = [
        'apple.png', 'check-l1.png', 'check-l2.png', 
        'check-l3.png', 'check-l4.png'
    ]
    
    sequence2 = [
        'check-gusetid.png', 'check-gusetid1.png',
        'check-l1.png', 'check-l2.png', 'check-l3.png', 'check-l4.png',
        'check-ok1.png', 'check-ok2.png', 'check-ok3.png', 'check-ok4.png'
    ]
    
    # ⭐ ทำ Sequence 1
    print(f"{Fore.CYAN}[DEVICE {device.serial}] Processing sequence 1...{Style.RESET_ALL}")
    for img_name in sequence1:
        timeout = 8  # ⚡ ลดจาก 15s
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                cap = device.screencap()
                image = np.frombuffer(cap, dtype=np.uint8)
                adb_img = cv2.imdecode(image, cv2.IMREAD_COLOR)
                
                pos = ImgSearchADB(adb_img, f'img/{img_name}')
                if pos:
                    print(f"{Fore.GREEN}[DEVICE {device.serial}] Found {img_name}{Style.RESET_ALL}")
                    device.shell(f"input tap {pos[0][0]} {pos[0][1]}")
                    time.sleep(0.3)  # � MSS: เร็วขึ้น
                    break
                    
                time.sleep(0.8)  # ⭐ PERF: ลดจาก 0.5 เป็น 0.8 ลดโหลด MuMu
            except Exception:
                continue
    
    # กด BACK หลัง sequence1
    time.sleep(1)  # ⚡ ลดจาก 2s
    device.shell("input keyevent 4")
    time.sleep(0.5)  # ⚡ ลดจาก 1s
    
    # ⭐ ทำ Sequence 2
    print(f"{Fore.CYAN}[DEVICE {device.serial}] Processing sequence 2...{Style.RESET_ALL}")
    for img_name in sequence2:
        timeout = 8  # ⚡ ลดจาก 15s
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                cap = device.screencap()
                image = np.frombuffer(cap, dtype=np.uint8)
                adb_img = cv2.imdecode(image, cv2.IMREAD_COLOR)
                
                pos = ImgSearchADB(adb_img, f'img/{img_name}')
                if pos:
                    print(f"{Fore.GREEN}[DEVICE {device.serial}] Found {img_name}{Style.RESET_ALL}")
                    device.shell(f"input tap {pos[0][0]} {pos[0][1]}")
                    time.sleep(0.3)  # � MSS: เร็วขึ้น
                    break
                    
                time.sleep(0.8)  # ⭐ PERF: ลดจาก 0.5 เป็น 0.8 ลดโหลด MuMu
            except Exception:
                continue
    
    print(f"{Fore.GREEN}[DEVICE {device.serial}] Apple Sequence completed!{Style.RESET_ALL}")
    return True



def main_login(device):
    stoplogin_count = 0
    fixbug_timer = None
    alert2_timer = None
    no_image_timer = None
    checkline_timer = None
    black_screen_timer = None
    fixid_count = 0
    refresh_count = 0
    back_press_mode = False
    last_processed_time = time.time()
    checkline_sequence_active = False
    checkline_sequence_index = 0
    last_found_time = time.time()
    last_image = None
    # ⭐ ADB STABLE MODE: ลด interval เพื่อไม่ให้ MuMu ช้า
    image_check_interval = 1.0  # ⭐ เพิ่มจาก 0.5 เป็น 1.0 วินาที ลด screencap load
    loop_count = 0
    consecutive_no_image = 0

    while True:
        try:
            loop_count += 1
            current_time = time.time()
            
            # ⭐ Clean memory ทุก 200 loops
            if loop_count % 200 == 0:
                clean_memory()
                loop_count = 0
            
            # ⭐ ADAPTIVE: ถ้าไม่เจอรูปนานให้รอนานขึ้น ลด CPU + ลดโหลด MuMu
            if consecutive_no_image > 20:
                adaptive_interval = min(3.0, image_check_interval + 1.0)  # ⭐ รอนานขึ้นถ้าไม่เจออะไรเลย
            elif consecutive_no_image > 10:
                adaptive_interval = min(2.5, image_check_interval + 0.5)
            else:
                adaptive_interval = image_check_interval
            
            if current_time - last_found_time >= adaptive_interval:
                # ⭐ ลบ image เก่าทันที
                last_image = None
                adb_img = get_screen_capture(device)
                if adb_img is None:
                    time.sleep(1.0)  # ⭐ เพิ่มจาก 0.5 เป็น 1.0 ลดโหลด MuMu
                    continue
                last_found_time = current_time
            else:
                # ⭐ ถ้ายังไม่ถึงเวลา ให้รอก่อน ไม่ต้องทำอะไร
                time.sleep(0.1)
                continue

            found_any_image = False

            if check_black_screen(adb_img, threshold=0.8):
                if black_screen_timer is None:
                    black_screen_timer = time.time()
                else:
                    elapsed = time.time() - black_screen_timer
                    if elapsed >= 60:
                        clear_app(device)
                        time.sleep(3)
                        device.shell("am start -n com.linecorp.LGRGS/com.linecorp.common.activity.LineActivity")
                        time.sleep(2)
                        black_screen_timer = None
                        continue
            else:
                black_screen_timer = None

            stopcheck_pos = check_stopcheck_with_multiple_thresholds(adb_img)
            if stopcheck_pos:
                clear_app(device)
                time.sleep(2)
                return "stopcheck_complete"

            apple_pos = ImgSearchADB(adb_img, 'img/apple.png')
            if apple_pos:
                found_any_image = True
                print(f"{Fore.YELLOW}[DEVICE {device.serial}] Found apple.png! Running sequence first...{Style.RESET_ALL}")
                # ⭐ STEP 1: ทำ sequence1 และ sequence2 ก่อน
                handle_apple_sequence(device)
                # ⭐ STEP 2: clear app
                clear_app(device)
                time.sleep(0.5)
                # ⭐ STEP 3: return เพื่อฉีดไฟล์ใหม่
                return "apple_inject_new_file"

            checkline_pos = ImgSearchADB(adb_img, 'img/checkline.png')
            if checkline_pos:
                found_any_image = True
                if not checkline_sequence_active:
                    if checkline_timer is None:
                        checkline_timer = time.time()
                    else:
                        elapsed = time.time() - checkline_timer
                        if elapsed >= 5:
                            checkline_sequence_active = True
                            checkline_sequence_index = 0
                            checkline_timer = None
            else:
                checkline_timer = None
            
            if checkline_sequence_active:
                sequence_images = ['check-l1.png', 'check-l2.png', 'check-l3.png', 'check-l4.png']
                
                if checkline_sequence_index < len(sequence_images):
                    img_name = sequence_images[checkline_sequence_index]
                    pos = ImgSearchADB(adb_img, f'img/{img_name}')
                    if pos:
                        found_any_image = True
                        device.shell(f"input tap {pos[0][0]} {pos[0][1]}")
                        checkline_sequence_index += 1
                        
                        # ⭐ FIX: Reset sequence เมื่อครบทุกรูป
                        if checkline_sequence_index >= len(sequence_images):
                            checkline_sequence_active = False
                            checkline_sequence_index = 0
                time.sleep(0.5)
                continue

            stoplogin_pos = ImgSearchADB(adb_img, 'img/stoplogin.png')
            stoploginnew_pos = ImgSearchADB(adb_img, 'img/stoploginnew.png')
            
            if stoplogin_pos or stoploginnew_pos:
                found_any_image = True
                stoplogin_count += 1
                if stoplogin_count >= 1:
                    if backup_to_login_send(device):
                        ui_stats.update(processed=ui_stats.processed_files + 1)
                    return "normal_complete"
                continue

            
            alert2_pos = ImgSearchADB(adb_img, 'img/alert2.png')
            if alert2_pos:
                found_any_image = True
                if alert2_timer is None:
                    alert2_timer = time.time()
                else:
                    elapsed_time = time.time() - alert2_timer
                    if elapsed_time >= 30:
                        clear_app(device)
                        time.sleep(0.2)
                        alert2_timer = None
                        continue
            else:
                alert2_timer = None

            alert3_pos = ImgSearchADB(adb_img, 'img/alert3.png')
            if alert3_pos:
                found_any_image = True
                clear_app(device)
                time.sleep(0.2)
                continue

            fixid_pos = ImgSearchADB(adb_img, 'img/fixid.png')
            if fixid_pos:
                found_any_image = True
                fixid_count += 1
                device.shell(f"input tap {fixid_pos[0][0]} {fixid_pos[0][1]}")

                ok_pos = ImgSearchADB(adb_img, 'img/ok.png')
                if ok_pos:
                    device.shell(f"input tap {ok_pos[0][0]} {ok_pos[0][1]}")

                if fixid_count >= 10:
                    if save_fail(device):
                        clear_app(device)
                        time.sleep(0.5)
                        return "restart"
                    else:
                        return "restart"
                continue

            refresh_pos = ImgSearchADB(adb_img, 'img/refresh.png')
            if refresh_pos:
                found_any_image = True
                refresh_count += 1

                if refresh_count >= 8:
                    refresh_count = 0
                else:
                    device.shell(f"input tap {refresh_pos[0][0]} {refresh_pos[0][1]}")
                    new_img = get_screen_capture(device)
                    if new_img is not None:
                        check_pos = ImgSearchADB(new_img, 'img/check.png')
                        if check_pos:
                            device.shell(f"input tap {check_pos[0][0]} {check_pos[0][1]}")
                continue

            if not refresh_pos:
                refresh_count = 0

            fixbug_pos = ImgSearchADB(adb_img, 'img/fixbuglogin.png')
            if fixbug_pos:
                found_any_image = True
                if fixbug_timer is None:
                    fixbug_timer = time.time()
                else:
                    elapsed_time = time.time() - fixbug_timer
                    if elapsed_time >= 13:
                        clear_app(device)
                        time.sleep(0.5)
                        fixbug_timer = None
            else:
                fixbug_timer = None

            link1_pos = ImgSearchADB(adb_img, 'img/link1.png')
            if link1_pos:
                found_any_image = True
                device.shell(f"input tap {link1_pos[0][0]} {link1_pos[0][1]}")
                device.shell("input text https://lg-release-tracking-8080.gcld-line.com/tracking/v1.0/link/LGRGS/TRACKING-LINK-LGRGS-446d2883-5ac3-4c36-ba54-12045346e90c/click")
                device.shell("input keyevent KEYCODE_ENTER")
                no_image_timer = None

            event_pos = ImgSearchADB(adb_img, 'img/event.png')
            if event_pos:
                found_any_image = True
                device.shell(f"input tap {event_pos[0][0]} {event_pos[0][1]}")
                no_image_timer = None
                back_press_mode = True

                back_press_count = 0
                stoplogin_check_count = 0
                
                while back_press_mode:
                    device.shell("input keyevent KEYCODE_BACK")
                    device.shell("input keyevent KEYCODE_BACK")
                    device.shell("input keyevent KEYCODE_BACK")
                    back_press_count += 3

                    stoplogin_check_count += 1
                    if stoplogin_check_count >= 5:
                        check_img = get_screen_capture(device)
                        if check_img is not None:
                            quick_stopcheck = check_stopcheck_with_multiple_thresholds(check_img)
                            if quick_stopcheck:
                                clear_app(device)
                                time.sleep(2)
                                return "stopcheck_complete"

                            quick_stoplogin = ImgSearchADB(check_img, 'img/stoplogin.png')
                            quick_stoploginnew = ImgSearchADB(check_img, 'img/stoploginnew.png')
                            
                            if quick_stoplogin or quick_stoploginnew:
                                if backup_to_login_send(device):
                                    ui_stats.update(processed=ui_stats.processed_files + 1)
                                return "normal_complete"

                            cancel_pos = ImgSearchADB(check_img, 'img/cancel.png')
                            if cancel_pos:
                                device.shell(f"input tap {cancel_pos[0][0]} {cancel_pos[0][1]}")
                                back_press_mode = False
                                break
                            
                        stoplogin_check_count = 0

                    if back_press_count > 100:
                        back_press_mode = False
                        break

            # ⭐ แบ่ง general_images เป็น 2 กลุ่ม: สำคัญ (เช็คทุกครั้ง) และ รอง (เช็คสลับ)
            priority_images = [
                ('test.png', 'test'),
                ('fixcak.png', 'fixcak'),
                ('ok.png', 'ok'),
                ('closeapp.png', 'closeapp'),
            ]
            
            secondary_images = [
                ('check.png', 'check'),
                ('okwhite.png', 'okwhite'),
                ('fixnet.png', 'fixnet'),
                ('fixplay.png', 'fixplay'),
                ('oknet.png', 'oknet'),
                ('fixalerterror1.png','fixalerterror1'),
                ('fixback.png', 'fixback'),
                ('fixout.png', 'fixout'),
                ('alert1.png', 'alert1'),
                ('oken.png', 'oken'),
                ('fixok.png', 'fixok')
            ]

            # ⭐ เช็ค priority ก่อนเสมอ
            for img_name, img_key in priority_images:
                pos = ImgSearchADB(adb_img, f'img/{img_name}')
                if pos:
                    found_any_image = True
                    device.shell(f"input tap {pos[0][0]} {pos[0][1]}")
                    no_image_timer = None
                    break
            
            # ⭐ เช็ค secondary เฉพาะเมื่อไม่เจอ priority (ลด CPU 50%)
            if not found_any_image:
                # สลับเช็ค secondary ทีละ 3-4 ตัว (ไม่เช็คทั้งหมดทุก loop)
                batch_size = 4
                start_idx = (loop_count % max(1, len(secondary_images) // batch_size)) * batch_size
                batch = secondary_images[start_idx:start_idx + batch_size]
                
                for img_name, img_key in batch:
                    pos = ImgSearchADB(adb_img, f'img/{img_name}')
                    if pos:
                        found_any_image = True
                        device.shell(f"input tap {pos[0][0]} {pos[0][1]}")
                        no_image_timer = None
                        break

            # ⭐ PERFORMANCE: ลดโหลด MuMu ด้วย adaptive sleep
            if not found_any_image:
                consecutive_no_image += 1  # นับครั้งที่ไม่เจอรูป
                if no_image_timer is None:
                    no_image_timer = time.time()
                else:
                    elapsed_time = time.time() - no_image_timer
                    if elapsed_time >= 400:
                        clear_app(device)
                        no_image_timer = None
                        time.sleep(1.0)
                
                # ⭐ FIXED: รอนานขึ้นเพื่อไม่ให้ MuMu ช้า
                if consecutive_no_image > 30:
                    time.sleep(2.0)  # ⭐ รอ 2 วินาที ถ้าไม่เจออะไรนานมาก
                elif consecutive_no_image > 20:
                    time.sleep(1.5)
                elif consecutive_no_image > 10:
                    time.sleep(1.0)
                else:
                    time.sleep(0.5)  # ⭐ เพิ่มจาก 0 (ไม่มี sleep เลย)
            else:
                consecutive_no_image = 0
                no_image_timer = None
                time.sleep(0.5)  # ⭐ เพิ่มจาก 0.2 ลดโหลดหลังกดรูป

            # ⭐ Clean memory ทุก 60 วินาที
            if current_time - last_processed_time > 60:
                clean_memory()
                last_processed_time = current_time

        except Exception:
            time.sleep(0.2)  # � MSS: เร็วขึ้น
            pass

    return False



def process_single_file_for_device(device):
    try:
        update_file_queue()
        
        if device_state.file_queue.empty():
            return False

        xml_file = None
        with device_state.lock:
            if not device_state.file_queue.empty():
                xml_file = device_state.file_queue.get()
                
                if xml_file in device_state.processed_files:
                    print(f"{Fore.YELLOW}[DEVICE {device.serial}] File {xml_file} already processed, skipping{Style.RESET_ALL}")
                    return False
                
                device_state.processed_files.add(xml_file)
                device_state.original_filenames[device.serial] = xml_file

        if not xml_file:
            return False

        original_file_path = os.path.join(source_folder, xml_file)
        
        if not os.path.exists(original_file_path):
            print(f"{Fore.YELLOW}[DEVICE {device.serial}] File {xml_file} not found, may have been processed{Style.RESET_ALL}")
            return False

        # ⭐ FIX: สร้าง temp file ใน OS temp dir แทน backup folder เพื่อไม่ให้ queue หยิบไป process!
        import tempfile
        safe_serial = str(device.serial).replace(':', '_').replace('.', '_')
        device_specific_temp_file = f"_LINE_COCOS_PREF_KEY_{safe_serial}_{int(time.time())}.xml"
        temp_file_path = os.path.join(tempfile.gettempdir(), device_specific_temp_file)

        try:
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    time.sleep(0.1)
                except:
                    pass

            max_retries = 2
            for attempt in range(max_retries):
                try:
                    shutil.copy2(original_file_path, temp_file_path)
                    break
                except Exception as e:
                    if attempt == max_retries - 1:
                        print(f"{Fore.RED}[DEVICE {device.serial}] Failed to copy file: {str(e)}{Style.RESET_ALL}")
                        with device_state.lock:
                            device_state.processed_files.discard(xml_file)
                        return False
                    time.sleep(0.2)

            # ⭐ ไม่ลบไฟล์ต้นฉบับที่นี่ - ให้ move_to_return จัดการย้ายไป return folder หลัง login เสร็จ
            # try:
            #     os.remove(original_file_path)
            # except Exception as e:
            #     pass

            destination_path = "/data/data/com.linecorp.LGRGS/shared_prefs/"
            
            # enable_root(device) # CAUSES DISCONNECT
            # time.sleep(0.5)
            
            device.shell(f"su -c 'rm -f {destination_path}_LINE_COCOS_PREF_KEY.xml'")
            device.shell(f"su -c 'mkdir -p {destination_path}'")
            device.shell(f"su -c 'chmod 777 {destination_path}'")
            
            push_success = False
            for attempt in range(max_retries):
                try:
                    # Strategy: Push to /data/local/tmp/ (Writable) -> Move to Target (Root)
                    tmp_remote_path = f"/data/local/tmp/tmp_{safe_serial}.xml"
                    
                    push_command = f'adb -s {device.serial} push "{temp_file_path}" "{tmp_remote_path}"'
                    
                    with adb_push_semaphore:
                        result = subprocess.run(
                            push_command, 
                            shell=True, 
                            stdout=subprocess.PIPE, 
                            stderr=subprocess.PIPE, 
                            text=True,
                            timeout=45 # Increased timeout
                        )
                    
                    if result.returncode == 0:
                        # Move file using root
                        mv_cmd = f"su -c 'mv -f {tmp_remote_path} {destination_path}_LINE_COCOS_PREF_KEY.xml'"
                        device.shell(mv_cmd)
                        
                        # Verify
                        chk_res = device.shell(f"su -c 'ls {destination_path}_LINE_COCOS_PREF_KEY.xml'")
                        if "No such file" not in chk_res:
                            push_success = True
                            break
                    
                    if attempt < max_retries - 1:
                        time.sleep(1)
                    else:
                        err_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                        print(f"{Fore.RED}[DEVICE {device.serial}] Push failed (Code {result.returncode}): {err_msg}{Style.RESET_ALL}")

                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(1)
                    else:
                        print(f"{Fore.RED}[DEVICE {device.serial}] Push exception: {str(e)}{Style.RESET_ALL}")
            
            if push_success:
                device.shell(f"su -c 'chmod 666 {destination_path}_LINE_COCOS_PREF_KEY.xml'")
            
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except:
                    pass

            if push_success:
                return True
            else:
                with device_state.lock:
                    device_state.processed_files.discard(xml_file)
                return False

        except Exception as e:
            print(f"{Fore.RED}[DEVICE {device.serial}] Error processing file: {str(e)}{Style.RESET_ALL}")
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except:
                    pass
            with device_state.lock:
                device_state.processed_files.discard(xml_file)
            return False

    except Exception as e:
        print(f"{Fore.RED}[DEVICE {device.serial}] Exception in process_single_file: {str(e)}{Style.RESET_ALL}")
        return False

def process_single_device(device):
    device_serial = device.serial
    force_restart_first_loop = False # Flag to force first loop execution
    
    # ⭐ DEBUG: แสดงว่า thread เริ่มทำงาน
    print(f"{Fore.GREEN}[DEVICE {device.serial}] ★★★ Thread started! ★★★{Style.RESET_ALL}")
    
    clear_app(device)
    time.sleep(1)  # ⚡ ลดจาก 2s
    
    config = read_config()
    
    # ⭐ DEBUG: แสดงค่า config
    print(f"{Fore.CYAN}[DEVICE {device.serial}] Config loop1 = {config.get('loop1', 1)}{Style.RESET_ALL}")
    
    with device_state.lock:
        if device_serial not in device_state.device_first_loop:
            device_state.device_first_loop[device_serial] = False
    
    while True:
        try:
            # Check if we should run first loop (if configured OR forced)
            should_run_first_loop = not device_state.device_first_loop[device_serial] and (config.get("loop1", 1) == 1 or force_restart_first_loop)

            if should_run_first_loop:
                print(f"{Fore.CYAN}[DEVICE {device.serial}] Starting first loop process (Forced: {force_restart_first_loop}){Style.RESET_ALL}")
                
                while True:
                    result = first_loop_process(device)
                    if result == "complete":
                        with device_state.lock:
                            device_state.device_first_loop[device_serial] = True
                        force_restart_first_loop = False # Reset force flag
                        break
                    elif result == "restart_from_test":
                        continue
                    elif result == "restart_first_loop":
                        continue
                    else:
                        time.sleep(5)
                        continue
            elif not device_state.device_first_loop[device_serial] and config.get("loop1", 1) == 0:
                with device_state.lock:
                    device_state.device_first_loop[device_serial] = True
            
            if not has_xml_files():
                time.sleep(1)
                continue

            file_processed = process_single_file_for_device(device)
            
            if file_processed:
                # ⚡ TURBO: เปิดแอปหลังจากส่งไฟล์สำเร็จ ก่อนที่จะ login
                print(f"{Fore.CYAN}[DEVICE {device.serial}] Opening app before login...{Style.RESET_ALL}")
                device.shell("am start -n com.linecorp.LGRGS/com.linecorp.common.activity.LineActivity")
                time.sleep(1.5)  # ⚡ ลดจาก 2s
                
                login_result = main_login(device)
                
                # ⭐ เก็บชื่อไฟล์เก่าไว้ก่อน เพื่อใช้ย้าย/ลบหลัง login
                old_filename = device_state.original_filenames.get(device.serial)
                
                if login_result == "restart_first_loop":
                    print(f"[DEVICE {device.serial}] Resetting first loop requested (apple.png found).")
                    # ⭐ ย้ายไฟล์ไป return folder เพื่อไม่ให้มาวน loop ซ้ำ
                    move_to_return(device)
                    # ⭐ FIX: ลบชื่อไฟล์เก่าออกจาก processed_files
                    if old_filename:
                        with device_state.lock:
                            device_state.processed_files.discard(old_filename)
                    with device_state.lock:
                        device_state.device_first_loop[device_serial] = False
                    force_restart_first_loop = True # Set flag to force execution next loop
                    continue
                
                # ⭐ NEW: จัดการ apple.png - ข้าม first_loop ฉีดไฟล์ใหม่เลย!
                if login_result == "apple_inject_new_file":
                    print(f"{Fore.YELLOW}[DEVICE {device.serial}] Apple detected! Moving to return and injecting new file...{Style.RESET_ALL}")
                    # ⭐ STEP 1: ย้ายไฟล์เก่าไป return folder
                    move_to_return(device)
                    
                    # ⭐ STEP 2: ลบชื่อไฟล์เก่าออกจาก processed_files เพื่อให้หยิบไฟล์ใหม่ได้!
                    if old_filename:
                        with device_state.lock:
                            device_state.processed_files.discard(old_filename)
                        print(f"{Fore.CYAN}[DEVICE {device.serial}] Cleared '{old_filename}' from processed_files{Style.RESET_ALL}")
                    
                    ui_stats.update(fail=ui_stats.failed_logins + 1)
                    
                    # ⭐ STEP 3: ลบไฟล์ _LINE_COCOS_PREF_KEY.xml เก่าออกจาก device ก่อน!
                    device.shell("su -c 'rm -f /data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml'")
                    print(f"{Fore.CYAN}[DEVICE {device.serial}] Deleted old _LINE_COCOS_PREF_KEY.xml from device{Style.RESET_ALL}")
                    
                    # ⭐ STEP 4: clear app เพื่อเตรียมรับไฟล์ใหม่
                    clear_app(device)
                    time.sleep(0.5)
                    
                    # (device_first_loop ยังเป็น True อยู่ = ข้าม first_loop)
                    continue  # กลับไป loop ใหม่ หยิบไฟล์ใหม่ แล้วไป main_login
                
                # ⭐ เพิ่มการจัดการ login สำเร็จ (normal_complete)
                if login_result == "normal_complete":
                    print(f"{Fore.GREEN}[DEVICE {device.serial}] Login completed! Clearing app and continuing...{Style.RESET_ALL}")
                    with device_state.lock:
                        device_state.success_count += 1
                    ui_stats.update(success=device_state.success_count)
                    # ไฟล์ถูกส่งไป login-send แล้วโดย backup_to_login_send()
                
                # ⭐ เพิ่มการจัดการ stopcheck_complete
                elif login_result == "stopcheck_complete":
                    print(f"{Fore.YELLOW}[DEVICE {device.serial}] Stopcheck detected! Moving file and continuing...{Style.RESET_ALL}")
                    with device_state.lock:
                        device_state.fail_count += 1
                    ui_stats.update(fail=device_state.fail_count)
                
                # ⭐ เพิ่มการจัดการ restart (fixid found)
                elif login_result == "restart":
                    print(f"{Fore.YELLOW}[DEVICE {device.serial}] Login failed (restart). Clearing app and continuing...{Style.RESET_ALL}")
                    with device_state.lock:
                        device_state.fail_count += 1
                    ui_stats.update(fail=device_state.fail_count)
                    # ไฟล์ถูกส่งไป login-fail แล้วโดย save_fail()
                
                # ⭐⭐⭐ FIX หลัก: ย้ายไฟล์ต้นฉบับออก + ลบจาก processed_files ทุกกรณี!
                # เพื่อให้ device หยิบไฟล์ใหม่ได้ในรอบถัดไป
                move_to_return(device)
                if old_filename:
                    with device_state.lock:
                        device_state.processed_files.discard(old_filename)
                    print(f"{Fore.CYAN}[DEVICE {device.serial}] ✓ File '{old_filename}' moved & cleared → ready for next file{Style.RESET_ALL}")
                
                clear_app(device)
                time.sleep(0.3)  # ⚡ TURBO: ลดจาก 1s เหลือ 0.3s
                
            else:
                time.sleep(0.5)  # ⭐ เพิ่มจาก 0.1 เพื่อลดโหลด

        except Exception as e:
            print(f"{Fore.RED}[DEVICE {device.serial}] Error in main loop: {str(e)}{Style.RESET_ALL}")
            time.sleep(0.3)  # ⚡ TURBO

def enable_root(device):
    # adb root causes network emulators to support connection
    # We use su -c chmod 777 instead, so this is not needed and dangerous
    pass

def reconnect_device(serial):
    try:
        subprocess.run(["adb", "connect", serial], timeout=3, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except:
        pass

def start_adb_server():
    try:
        kill_existing_adb()
        result = subprocess.run(["adb", "start-server"], 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE, 
                              text=True)
        
        if result.returncode == 0:
            print(f"{Fore.GREEN}[INFO] ADB server started successfully{Style.RESET_ALL}")
            return True
        else:
            # ⭐ เพิ่ม path สำหรับ MuMu Player หลายรุ่น
            mumu_adb_paths = [
                "F:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe",
                "C:\\Program Files\\Netease\\MuMuPlayerGlobal-12.0\\shell\\adb.exe",
                "C:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe"
            ]
            
            for mumu_adb_path in mumu_adb_paths:
                if os.path.exists(mumu_adb_path):
                    print(f"{Fore.CYAN}[INFO] Trying ADB at: {mumu_adb_path}{Style.RESET_ALL}")
                    result = subprocess.run([mumu_adb_path, "start-server"],
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE,
                                         text=True)
                    if result.returncode == 0:
                        print(f"{Fore.GREEN}[INFO] ADB server started with MuMu ADB{Style.RESET_ALL}")
                        return True
            
            print(f"{Fore.RED}[ERROR] Failed to start ADB server{Style.RESET_ALL}")
            return False
            
    except Exception as e:
        print(f"{Fore.RED}[ERROR] start_adb_server: {str(e)}{Style.RESET_ALL}")
        return False

def kill_existing_adb():
    try:
        if os.name == 'nt':
            subprocess.run(["taskkill", "/F", "/IM", "adb.exe"], 
                         stdout=subprocess.PIPE, 
                         stderr=subprocess.PIPE)
        else:
            subprocess.run(["killall", "adb"], 
                         stdout=subprocess.PIPE, 
                         stderr=subprocess.PIPE)
        time.sleep(0.1)
    except Exception:
        pass

def check_adb_available():
    try:
        result = subprocess.run(["adb", "version"], 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE, 
                              text=True)
        
        if result.returncode == 0:
            if start_adb_server():
                time.sleep(0.5)
                return True
            return False

        # ⭐ เพิ่ม path สำหรับ MuMu Player หลายรุ่น
        mumu_adb_paths = [
            "F:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe",
            "C:\\Program Files\\Netease\\MuMuPlayerGlobal-12.0\\shell\\adb.exe",
            "C:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe",
            "F:\\MuMuPlayerGlobal-12.0\\shell\\adb.exe",
            "D:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe",
            "E:\\Program Files\\Netease\\MuMuPlayer\\shell\\adb.exe"
        ]
        
        for mumu_adb_path in mumu_adb_paths:
            if os.path.exists(mumu_adb_path):
                print(f"{Fore.GREEN}[INFO] Found ADB at: {mumu_adb_path}{Style.RESET_ALL}")
                os.environ["PATH"] = os.environ["PATH"] + os.pathsep + os.path.dirname(mumu_adb_path)
                
                result = subprocess.run(["adb", "version"], 
                                     stdout=subprocess.PIPE, 
                                     stderr=subprocess.PIPE, 
                                     text=True)
                
                if result.returncode == 0:
                    if start_adb_server():
                        time.sleep(0.5)
                        return True
                return False

        print(f"{Fore.RED}[ERROR] ADB not found in any location{Style.RESET_ALL}")
        return False

    except Exception as e:
        print(f"{Fore.RED}[ERROR] check_adb_available: {str(e)}{Style.RESET_ALL}")
        return False



# ⭐ FIXED: ฟังก์ชัน connect_to_mumu ที่สแกนหา port อัตโนมัติ
def connect_to_mumu():
    try:
        subprocess.run(["adb", "kill-server"], capture_output=True, timeout=3)
        time.sleep(0.1)
        
        mumu_path = get_mumu_path()
        if mumu_path:
            os.environ["PATH"] = os.environ["PATH"] + os.pathsep + mumu_path
        
        subprocess.run(["adb", "start-server"], capture_output=True, timeout=3)
        time.sleep(0.5)

        # ⭐ ขั้นตอนที่ 1: ตรวจสอบ devices ที่เชื่อมต่ออยู่แล้ว
        print(f"{Fore.CYAN}[INFO] Checking already connected devices...{Style.RESET_ALL}")
        adb = AdbClient(host="127.0.0.1", port=5037)
        
        try:
            existing_devices = adb.devices()
            if existing_devices:
                print(f"{Fore.GREEN}[INFO] Found {len(existing_devices)} device(s) already connected!{Style.RESET_ALL}")
                for device in existing_devices:
                    print(f"{Fore.GREEN}  ✓ {device.serial}{Style.RESET_ALL}")
                return adb, existing_devices if len(existing_devices) > 1 else existing_devices[0]
        except Exception as e:
            print(f"{Fore.YELLOW}[WARNING] Could not get existing devices: {str(e)}{Style.RESET_ALL}")

        # ⭐ ขั้นตอนที่ 2: Auto-scan ports
        print(f"{Fore.CYAN}[INFO] Auto-scanning ports from {START_PORT} ({MAX_DEVICES} devices)...{Style.RESET_ALL}")

        connected_devices = []
        
        def try_connect_port(port):
            try:
                result = subprocess.run(
                    ["adb", "connect", f"127.0.0.1:{port}"],
                    capture_output=True,
                    timeout=2,
                    text=True
                )
                time.sleep(0.3)
                
                # ตรวจสอบว่าเชื่อมต่อสำเร็จหรือไม่
                if "connected" in result.stdout.lower() or "already connected" in result.stdout.lower():
                    # ดึง device object
                    try:
                        devices = adb.devices()
                        for device in devices:
                            if f":{port}" in device.serial or f"emulator-{port}" in device.serial:
                                print(f"{Fore.GREEN}  ✓ Connected: 127.0.0.1:{port} ({device.serial}){Style.RESET_ALL}")
                                return device
                    except Exception:
                        pass
            except Exception:
                pass
            return None

        # ⭐ สแกน port ทีละตัวแบบ parallel เพื่อความเร็ว
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(try_connect_port, port): port for port in MUMU_PORTS}
            
            for future in concurrent.futures.as_completed(futures):
                device = future.result()
                if device:
                    connected_devices.append(device)

        if connected_devices:
            print(f"\n{Fore.GREEN}[SUCCESS] Total {len(connected_devices)} device(s) connected!{Style.RESET_ALL}")
            for i, device in enumerate(connected_devices, 1):
                print(f"{Fore.GREEN}  Device {i}: {device.serial}{Style.RESET_ALL}")
            return adb, connected_devices if len(connected_devices) > 1 else connected_devices[0]
        
        print(f"{Fore.RED}[ERROR] No devices found. Please check if MuMu Player is running.{Style.RESET_ALL}")
        return None, []

    except Exception as e:
        print(f"{Fore.RED}[ERROR] connect_to_mumu exception: {str(e)}{Style.RESET_ALL}")
        return None, []



def update_file_queue():
    try:
        xml_files = []
        for root, dirs, files in os.walk(source_folder):
            # ⭐ FIX: กรองไฟล์ temp ออก! ไม่งั้นมันจะถูกหยิบมา process แล้วสร้าง temp ใหม่วนไม่สิ้นสุด
            xml_files.extend([f for f in files 
                             if f.endswith('.xml') and not f.startswith('_LINE_COCOS_PREF_KEY_')])
        
        total_files = len(xml_files)
        ui_stats.update(total=total_files)
        
        with device_state.lock:
            for xml_file in xml_files:
                if xml_file not in device_state.processed_files:
                    queue_list = list(device_state.file_queue.queue)
                    if xml_file not in queue_list:
                        device_state.file_queue.put(xml_file)
                    
        return total_files
    except Exception as e:
        print(f"{Fore.RED}Error updating file queue: {str(e)}{Style.RESET_ALL}")
        return 0



def get_mumu_path():
    possible_paths = [
        # ⭐ เพิ่ม path ของคุณด้านบนสุด
        "F:\\Program Files\\Netease\\MuMuPlayer\\shell",
        "F:\\Program Files\\Netease\\MuMuPlayer",
        "F:\\Program Files\\Netease\\MuMuPlayer\\nx_main",
        # Path เดิม
        "C:\\Program Files\\Netease\\MuMuPlayerGlobal-12.0\\shell",
        "C:\\Program Files\\Netease\\MuMuPlayer\\shell",
        "F:\\MuMuPlayerGlobal-12.0\\shell",
        "D:\\MuMuPlayerGlobal-12.0\\shell",
        "E:\\MuMuPlayerGlobal-12.0\\shell",
        "D:\\Program Files\\Netease\\MuMuPlayer\\shell",
        "E:\\Program Files\\Netease\\MuMuPlayer\\shell",
        os.path.join(os.environ.get('LOCALAPPDATA', ''), "Netease\\MuMuPlayerGlobal-12.0\\shell"),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), "Netease\\MuMuPlayer\\shell"),
        os.path.join(os.environ.get('PROGRAMFILES', ''), "Netease\\MuMuPlayerGlobal-12.0\\shell"),
        os.path.join(os.environ.get('PROGRAMFILES', ''), "Netease\\MuMuPlayer\\shell"),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), "Netease\\MuMuPlayerGlobal-12.0\\shell"),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), "Netease\\MuMuPlayer\\shell")
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            print(f"{Fore.GREEN}[INFO] Found MuMu path: {path}{Style.RESET_ALL}")
            return path
    
    print(f"{Fore.RED}[WARNING] MuMu path not found in standard locations{Style.RESET_ALL}")
    return None



def show_simple_menu():
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{Fore.CYAN}╔{'═' * 40}╗{Style.RESET_ALL}")
    print(f"{Fore.CYAN}║{' ' * 10}LINE RANGER TOOL{' ' * 11}║{Style.RESET_ALL}")
    print(f"{Fore.CYAN}╠{'═' * 40}╣{Style.RESET_ALL}")
    print(f"║ 1. Start Login Process             ║")
    print(f"║ 2. Move Files                      ║")
    print(f"║ 3. Exit                            ║")
    print(f"{Fore.CYAN}╚{'═' * 40}╝{Style.RESET_ALL}")
    
    while True:
        choice = input(f"\n{Fore.YELLOW}Select option (1-3): {Style.RESET_ALL}")
        if choice in ['1', '2', '3']:
            return choice
        print(f"{Fore.RED}Please select 1-3 only{Style.RESET_ALL}")

def handle_menu_choice(choice):
    backup_xml_dir, login_send_dir = check_directories()
    
    if choice == '1':
        backup_xml_count = count_xml_files(backup_xml_dir)
        ui_stats.update(total=backup_xml_count)
        
        if backup_xml_count == 0:
            print(f"{Fore.YELLOW}No XML files found in backup/backupxml{Style.RESET_ALL}")
            if count_xml_files(login_send_dir) > 0:
                print(f"{Fore.GREEN}Found XML files in login-send{Style.RESET_ALL}")
                user_choice = input("Move files from login-send to backup? (y/n): ")
                if user_choice.lower() == 'y':
                    copy_files_to_backup(login_send_dir, backup_xml_dir)
            else:
                print(f"{Fore.RED}No XML files found anywhere{Style.RESET_ALL}")
                return "menu"
        
        print(f"\n{Fore.GREEN}Starting login mode...{Style.RESET_ALL}")
        return "login"
        
    elif choice == '2':
        print(f"\n{Fore.YELLOW}Moving files...{Style.RESET_ALL}")
        copy_files_to_backup(login_send_dir, backup_xml_dir)
        print(f"{Fore.GREEN}Files moved successfully{Style.RESET_ALL}")
        input("Press Enter to continue...")
        return "menu"
        
    else:
        return "exit"

def copy_files_to_backup(login_send_dir, backup_xml_dir):
    try:
        xml_files = [f for f in os.listdir(login_send_dir) if f.endswith('.xml')]
        if not xml_files: 
            return False
            
        for xml_file in xml_files:
            src = os.path.join(login_send_dir, xml_file)
            dst = os.path.join(backup_xml_dir, xml_file)
            try:
                shutil.move(src, dst)
            except Exception: 
                continue
        return True
    except Exception: 
        return False

def check_directories():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    backup_xml = os.path.join(current_dir, "backup", "backupxml")
    login_send = os.path.join(current_dir, "login-send")
    
    for directory in [backup_xml, login_send]:
        if not os.path.exists(directory):
            try: 
                os.makedirs(directory)
            except: 
                pass
    
    return backup_xml, login_send

def check_mumu_running():
    """ตรวจสอบว่า MuMu Player กำลังทำงานอยู่หรือไม่"""
    try:
        # ⭐ วิธีที่ 1: ตรวจสอบจาก adb devices
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=3
            )
            if "emulator-" in result.stdout or "127.0.0.1:" in result.stdout:
                print(f"{Fore.GREEN}[INFO] Found device via ADB: {result.stdout.strip()}{Style.RESET_ALL}")
                return True
        except Exception as e:
            print(f"{Fore.YELLOW}[WARNING] ADB devices check failed: {str(e)}{Style.RESET_ALL}")
        
        # ⭐ วิธีที่ 2: ตรวจสอบจาก process name
        for proc in psutil.process_iter(['name']):
            proc_name = proc.info['name']
            if any(name in proc_name for name in ['MuMuPlayer', 'MuMu', 'NemuPlayer', 'nemu']):
                print(f"{Fore.GREEN}[INFO] Found MuMu process: {proc_name}{Style.RESET_ALL}")
                return True
        
        print(f"{Fore.RED}[WARNING] MuMu Player not detected{Style.RESET_ALL}")
        return False
    except Exception as e:
        print(f"{Fore.RED}[ERROR] check_mumu_running: {str(e)}{Style.RESET_ALL}")
        return False

def read_config():
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(current_dir, "config.json")
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return json.loads(f.read())
        return {"loop1": 1}
    except Exception:
        return {"loop1": 1}

def Main():
    print(f"{Fore.GREEN}Starting program...{Style.RESET_ALL}")
    auto_login = True
    
    # ⭐ เริ่ม Mini GUI สำหรับแสดงสถานะ
    if GUI_ENABLED:
        start_mini_gui()
        time.sleep(0.5)  # รอให้ GUI โหลดเสร็จ
    
    while True:
        try:
            if auto_login:
                choice = "1"
                auto_login = False
            else:
                choice = show_simple_menu()
            
            result = handle_menu_choice(choice)
            
            if result == "exit":
                break
            elif result == "menu":
                continue
            elif result == "login":
                retry_count = 0
                max_retries = 3
                
                ui_stats.force_update()
                
                while True:
                    try:
                        if not check_adb_available():
                            ui_stats.print_simple_message("ADB not found, retrying in 5 seconds...")
                            time.sleep(5)
                            clean_memory()
                            continue

                        if not check_mumu_running():
                            ui_stats.print_simple_message("Please start MuMu Player first")
                            time.sleep(5)
                            continue

                        adb, devices = connect_to_mumu()
                        
                        if not devices:
                            retry_count += 1
                            if retry_count >= max_retries:
                                ui_stats.print_simple_message(f"Cannot connect after {max_retries} retries")
                                retry_count = 0
                                time.sleep(10)
                                break
                            else:
                                ui_stats.print_simple_message(f"Connection retry {retry_count}/{max_retries}")
                                time.sleep(5)
                            continue
                        
                        device_count = len(devices) if isinstance(devices, list) else 1
                        ui_stats.update(devices=device_count)
                        
                        # ⭐ DEBUG: แสดงจำนวน devices ที่เชื่อมต่อ
                        print(f"{Fore.GREEN}★★★ Starting {device_count} device thread(s)... ★★★{Style.RESET_ALL}")
                        
                        if isinstance(devices, list):
                            threads = []
                            for device in devices:
                                print(f"{Fore.CYAN}  → Starting thread for {device.serial}{Style.RESET_ALL}")
                                thread = Thread(target=process_single_device, args=(device,))
                                thread.daemon = True
                                thread.start()
                                threads.append(thread)
                        else:
                            print(f"{Fore.CYAN}  → Starting thread for {devices.serial}{Style.RESET_ALL}")
                            thread = Thread(target=process_single_device, args=(devices,))
                            thread.daemon = True
                            thread.start()
                            threads = [thread]
                        
                        print(f"{Fore.GREEN}★★★ All threads started! Monitoring... ★★★{Style.RESET_ALL}")
                        
                        while True:
                            try:
                                cpu, mem = get_resource_usage()
                                if cpu > 90 or mem > 1024:
                                    clean_memory()
                                
                                time.sleep(30)
                                ui_stats.update()
                                
                                if not any(t.is_alive() for t in threads):
                                    print(f"{Fore.YELLOW}All device threads completed{Style.RESET_ALL}")
                                    print(f"{Fore.GREEN}Restarting loop...{Style.RESET_ALL}")
                                    # Force wait for all threads to finish
                                    for t in threads:
                                        t.join(timeout=1)
                                    time.sleep(0.5)
                                    # Reset state for next loop
                                    with device_state.lock:
                                        device_state.processed_files.clear()
                                        device_state.device_first_loop.clear()
                                        device_state.original_filenames.clear()
                                        while not device_state.file_queue.empty():
                                            try:
                                                device_state.file_queue.get_nowait()
                                            except:
                                                break
                                    # Reset UI stats counters
                                    ui_stats.update(success=0, fail=0, processed=0)
                                    clean_memory()
                                    gc.collect()
                                    time.sleep(1)
                                    break
                                    
                            except KeyboardInterrupt:
                                ui_stats.print_simple_message("Returning to main menu...")
                                clean_memory()
                                return
                        # Continue to next iteration without breaking from login loop
                        retry_count = 0

                    except KeyboardInterrupt:
                        ui_stats.print_simple_message("Returning to main menu...")
                        break
                    except Exception as e:
                        ui_stats.print_simple_message(f"Error: {str(e)}")
                        time.sleep(5)
                        clean_memory()

        except KeyboardInterrupt:
            continue
        except Exception as e:
            print(f"{Fore.RED}Error: {e}{Style.RESET_ALL}")
            time.sleep(5)

if __name__ == '__main__':
    try:
        Main()
    finally:
        colorama.deinit()
        if hasattr(device_state, 'sct'):
            device_state.sct.close()