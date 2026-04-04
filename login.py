import cv2
import numpy as np
import subprocess
import os

# ลดการแย่งชิง CPU สำหรับ OpenCV เมื่อรันหลายเครื่องพร้อมกัน
cv2.setNumThreads(1)
import time
from time import sleep
import sys
import shutil
import glob
import tempfile
import json
import threading
import queue
import concurrent.futures
import argparse
import colorama
from colorama import Fore, Style
import ssl
from datetime import datetime
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
import hashlib
import gc

# Try to import customtkinter for the modern UI
try:
    import customtkinter as ctk
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False
    print("[WARN] customtkinter not found. GUI mode will be disabled. Run 'pip install customtkinter' to enable.")

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

if GUI_AVAILABLE:
    class MainConfigWindow(ctk.CTkToplevel):
        """Window to edit config.json settings"""
        def __init__(self, parent):
            super().__init__(parent)
            self.title("⚙️ ตั้งค่า Config")
            self.geometry("550x650")
            self.parent = parent
            
            self.transient(parent)
            self.grab_set()
            self.focus_force()
            
            self.cfg = self.load_config()
            self.vars = {}
            
            scroll_frame = ctk.CTkScrollableFrame(self, width=500, height=500)
            scroll_frame.pack(fill="both", expand=True, padx=20, pady=10)
            
            ctk.CTkLabel(scroll_frame, text="🎮 ฟีเจอร์เกม", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(10, 5), anchor="w")
            
            self.add_switch(scroll_frame, "Loop1 (เปิดเกมครั้งแรก)", "first_loop")
            
            # Black Screen Timeout - ใส่ตัวเลข
            black_timeout_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            black_timeout_frame.pack(fill="x", padx=20, pady=5)
            ctk.CTkLabel(black_timeout_frame, text="TimeOut จอดำ (วินาที):", anchor="w").pack(side="left")
            self.black_timeout_entry = ctk.CTkEntry(black_timeout_frame, width=80)
            self.black_timeout_entry.insert(0, str(self.cfg.get("black_screen_timeout", 8)))
            self.black_timeout_entry.pack(side="left", padx=10)

            self.add_switch(scroll_frame, "7-Day (รับของ 7 วัน)", "7day")
            self.add_switch(scroll_frame, "แลกแต้มเขียว Leonard", "shopgacha")
            self.add_switch(scroll_frame, "สุ่มตัว (Swap Shop)", "swap_shop")
            self.add_switch(scroll_frame, "สุ่มตัว Event", "swap_shopevent")
            self.add_switch(scroll_frame, "⚡ หลบไก่บี้ (kaibyskip)", "kaibyskip")
            self.add_switch(scroll_frame, "⏩ ข้ามเช็คไก่บี้ (kaibycheck)", "kaibycheck")
            self.add_switch(scroll_frame, "ใช้ตั๋วทั้งหมด", "all-tiket")
            self.add_switch(scroll_frame, "ระบบ Link", "link")
            self.add_switch(scroll_frame, "ใช้เพชรในการสุ่ม", "all-in")
            
            # Max Gacha - ใส่ตัวเลข
            max_gacha_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            max_gacha_frame.pack(fill="x", padx=20, pady=5)
            ctk.CTkLabel(max_gacha_frame, text="จำนวนสุ่มสูงสุด (0=ไม่จำกัด):", anchor="w").pack(side="left")
            self.max_gacha_entry = ctk.CTkEntry(max_gacha_frame, width=80)
            self.max_gacha_entry.insert(0, str(self.cfg.get("max-gacha", 0)))
            self.max_gacha_entry.pack(side="left", padx=10)
            
            ctk.CTkFrame(scroll_frame, height=2, fg_color="gray30").pack(fill="x", pady=10)
            ctk.CTkLabel(scroll_frame, text="⚙️ ตั้งค่า Gear", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(5, 5), anchor="w")
            
            self.add_switch(scroll_frame, "Ruby-Gear 200", "ruby-gear200")
            self.add_switch(scroll_frame, "สุ่ม Gear", "random-gear")
            self.add_switch(scroll_frame, "ตรวจสอบ Gear", "check-gear")
            self.add_switch(scroll_frame, "ใช้ OCR (อ่านข้อความ)", "use_ocr")
            
            ctk.CTkFrame(scroll_frame, height=2, fg_color="gray30").pack(fill="x", pady=10)
            ctk.CTkLabel(scroll_frame, text="📦 ตั้งค่ากล่อง", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(5, 5), anchor="w")
            
            box_settings = self.cfg.get("box_settings", {})
            self.box_first_round = ctk.BooleanVar(value=bool(box_settings.get("first_round", 1)))
            self.box_second_round = ctk.BooleanVar(value=bool(box_settings.get("second_round", 1)))
            
            ctk.CTkSwitch(scroll_frame, text="รอบแรก", variable=self.box_first_round).pack(pady=5, padx=20, anchor="w")
            ctk.CTkSwitch(scroll_frame, text="รอบที่สอง", variable=self.box_second_round).pack(pady=5, padx=20, anchor="w")
            
            ctk.CTkFrame(scroll_frame, height=2, fg_color="gray30").pack(fill="x", pady=10)
            ctk.CTkLabel(scroll_frame, text="📡 ตั้งค่าช่อง", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(5, 5), anchor="w")
            
            self.channel_var = ctk.StringVar(value=self.cfg.get("channel", "ch2"))
            channel_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            channel_frame.pack(fill="x", padx=20, pady=5)
            ctk.CTkLabel(channel_frame, text="เลือกช่อง:").pack(side="left")
            channel_options = ["ch1", "ch2", "ch3", "ch4", "ch5"]
            ctk.CTkOptionMenu(channel_frame, variable=self.channel_var, values=channel_options, width=100).pack(side="left", padx=10)
            
            self.add_switch(scroll_frame, "ใช้รูปช่อง", "channels_img")
            
            # =============================================
            # ส่วนตั้งค่า Auto Trade
            # =============================================
            ctk.CTkFrame(scroll_frame, height=2, fg_color="gray30").pack(fill="x", pady=10)
            ctk.CTkLabel(scroll_frame, text="🛒 Auto Trade (ซื้อของ Swap Shop)", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(5, 5), anchor="w")
            
            auto_trade_cfg = self.cfg.get("auto_trade", {})
            self.auto_trade_enabled = ctk.BooleanVar(value=bool(auto_trade_cfg.get("enabled", 1)))
            ctk.CTkSwitch(scroll_frame, text="เปิดใช้งาน Auto Trade", variable=self.auto_trade_enabled).pack(pady=5, padx=20, anchor="w")
            
            # Shop1 - เพชร
            shop1_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            shop1_frame.pack(fill="x", padx=20, pady=3)
            ctk.CTkLabel(shop1_frame, text="💎 เพชร (swap_shop1):", anchor="w", width=180).pack(side="left")
            self.auto_trade_shop1 = ctk.CTkEntry(shop1_frame, width=60)
            self.auto_trade_shop1.insert(0, str(auto_trade_cfg.get("swap_shop1", 1)))
            self.auto_trade_shop1.pack(side="left", padx=5)
            ctk.CTkLabel(shop1_frame, text="ครั้ง", anchor="w").pack(side="left")
            
            # Shop2 - ตั๋ว
            shop2_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            shop2_frame.pack(fill="x", padx=20, pady=3)
            ctk.CTkLabel(shop2_frame, text="🎟️ ตั๋ว (swap_shop2):", anchor="w", width=180).pack(side="left")
            self.auto_trade_shop2 = ctk.CTkEntry(shop2_frame, width=60)
            self.auto_trade_shop2.insert(0, str(auto_trade_cfg.get("swap_shop2", 1)))
            self.auto_trade_shop2.pack(side="left", padx=5)
            ctk.CTkLabel(shop2_frame, text="ครั้ง", anchor="w").pack(side="left")
            
            # Shopkom - กบฟ้า
            shopkom_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            shopkom_frame.pack(fill="x", padx=20, pady=3)
            ctk.CTkLabel(shopkom_frame, text="🐸 กบฟ้า (swap_shopkom):", anchor="w", width=180).pack(side="left")
            self.auto_trade_shopkom = ctk.CTkEntry(shopkom_frame, width=60)
            self.auto_trade_shopkom.insert(0, str(auto_trade_cfg.get("swap_shopkom", 1)))
            self.auto_trade_shopkom.pack(side="left", padx=5)
            ctk.CTkLabel(shopkom_frame, text="ครั้ง", anchor="w").pack(side="left")
            
            # Shopkom9star - กบ9ดาว
            shopkom9_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            shopkom9_frame.pack(fill="x", padx=20, pady=3)
            ctk.CTkLabel(shopkom9_frame, text="⭐ กบ9ดาว (swap_shopkom9star):", anchor="w", width=180).pack(side="left")
            self.auto_trade_shopkom9star = ctk.CTkEntry(shopkom9_frame, width=60)
            self.auto_trade_shopkom9star.insert(0, str(auto_trade_cfg.get("swap_shopkom9star", 1)))
            self.auto_trade_shopkom9star.pack(side="left", padx=5)
            ctk.CTkLabel(shopkom9_frame, text="ครั้ง", anchor="w").pack(side="left")
            
            btn_frame = ctk.CTkFrame(self, fg_color="transparent")
            btn_frame.pack(fill="x", padx=20, pady=10)
            
            ctk.CTkButton(btn_frame, text="💾 บันทึก", command=self.save, fg_color="#2cc985", hover_color="#229f69", width=150).pack(side="left", padx=5)
            ctk.CTkButton(btn_frame, text="❌ ยกเลิก", command=self.destroy, fg_color="#555555", hover_color="#444444", width=100).pack(side="right", padx=5)
        
        def load_config(self):
            try:
                if os.path.exists('configmain.json'):
                    with open('configmain.json', 'r', encoding='utf-8') as f:
                        return json.load(f)
            except Exception as e:
                print(f"Error loading config: {e}")
            return {}
        
        def add_switch(self, parent, label, key):
            val = self.cfg.get(key, 0)
            var = ctk.BooleanVar(value=bool(val))
            self.vars[key] = var
            ctk.CTkSwitch(parent, text=label, variable=var).pack(pady=5, padx=20, anchor="w")
            
        def save(self):
            try:
                for key, var in self.vars.items():
                    self.cfg[key] = 1 if var.get() else 0
                
                if "box_settings" not in self.cfg:
                    self.cfg["box_settings"] = {}
                self.cfg["box_settings"]["first_round"] = 1 if self.box_first_round.get() else 0
                self.cfg["box_settings"]["second_round"] = 1 if self.box_second_round.get() else 0
                self.cfg["channel"] = self.channel_var.get()
                
                # Save max-gacha as number
                try:
                    self.cfg["max-gacha"] = int(self.max_gacha_entry.get())
                except:
                    self.cfg["max-gacha"] = 0
                
                # Save black_screen_timeout as number
                try:
                    self.cfg["black_screen_timeout"] = int(self.black_timeout_entry.get())
                except:
                    self.cfg["black_screen_timeout"] = 8
                
                # Save auto_trade settings
                if "auto_trade" not in self.cfg:
                    self.cfg["auto_trade"] = {}
                self.cfg["auto_trade"]["enabled"] = 1 if self.auto_trade_enabled.get() else 0
                try:
                    self.cfg["auto_trade"]["swap_shop1"] = int(self.auto_trade_shop1.get())
                except:
                    self.cfg["auto_trade"]["swap_shop1"] = 1
                try:
                    self.cfg["auto_trade"]["swap_shop2"] = int(self.auto_trade_shop2.get())
                except:
                    self.cfg["auto_trade"]["swap_shop2"] = 1
                try:
                    self.cfg["auto_trade"]["swap_shopkom"] = int(self.auto_trade_shopkom.get())
                except:
                    self.cfg["auto_trade"]["swap_shopkom"] = 1
                try:
                    self.cfg["auto_trade"]["swap_shopkom9star"] = int(self.auto_trade_shopkom9star.get())
                except:
                    self.cfg["auto_trade"]["swap_shopkom9star"] = 1
                
                with open('configmain.json', 'w', encoding='utf-8') as f:
                    json.dump(self.cfg, f, indent=4, ensure_ascii=False)
                
                messagebox.showinfo("สำเร็จ", "บันทึก Config เรียบร้อย!")
                try:
                    global load_config
                    load_config()
                except Exception as ex:
                    print(ex)
                self.parent.log("INFO", "✅ Config.json อัพเดทแล้ว")
                self.destroy()
            except Exception as e:
                messagebox.showerror("Error", f"บันทึกไม่สำเร็จ: {e}")


    class HeroConfigWindow(ctk.CTkToplevel):
        """
        หน้าต่างตั้งค่าชื่อ Ranger และ Gear
        HERO_MAPPING = ตั้งชื่อ Ranger ที่จะได้เมื่อพบรูป
        เช่น gachahero1.png พบแล้วจะตั้งชื่อไฟล์เป็น "som+"
        """
        def __init__(self, parent):
            super().__init__(parent)
            self.title("🦸 ตั้งชื่อ Ranger & Gear")
            self.geometry("600x700")
            self.parent = parent
            
            self.transient(parent)
            self.grab_set()
            self.focus_force()
            
            self.cfg = self.load_config()
            
            self.tabview = ctk.CTkTabview(self, width=550, height=550)
            self.tabview.pack(fill="both", expand=True, padx=20, pady=10)
            
            self.tabview.add("🦸 Rangers")
            self.tabview.add("⚙️ Gears")
            self.tabview.add("🔫 Weapons")
            
            self.setup_hero_tab()
            self.setup_gear_tab()
            self.setup_weapon_tab()
            
            ctk.CTkButton(self, text="💾 บันทึกทั้งหมด", command=self.save_all, fg_color="#2cc985", hover_color="#229f69").pack(pady=10)
        
        def load_config(self):
            try:
                if os.path.exists('configmain.json'):
                    with open('configmain.json', 'r', encoding='utf-8') as f:
                        return json.load(f)
            except Exception as e:
                print(f"Error loading config: {e}")
            return {}
        
        def setup_hero_tab(self):
            tab = self.tabview.tab("🦸 Rangers")
            
            # คำอธิบาย
            desc_frame = ctk.CTkFrame(tab, fg_color="#2b2b2b", corner_radius=8)
            desc_frame.pack(fill="x", padx=10, pady=(10, 5))
            ctk.CTkLabel(
                desc_frame, 
                text="📌 ตั้งชื่อ Ranger ที่จะบันทึก\\n📂 รูปอยู่ที่: img/ranger/gachaheroX.png\\n💡 เปลี่ยนรูปได้ง่าย แค่วางไฟล์ใหม่ทับ", 
                font=ctk.CTkFont(size=11),
                text_color="gray",
                justify="left"
            ).pack(padx=10, pady=5)
            
            ctk.CTkLabel(tab, text="รูป → ชื่อ Ranger", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=5)
            
            self.hero_entries = {}
            hero_mapping = self.cfg.get("HERO_MAPPING", {})
            
            scroll = ctk.CTkScrollableFrame(tab, width=480, height=300)
            scroll.pack(fill="both", expand=True, padx=10)
            
            for img, name in hero_mapping.items():
                frame = ctk.CTkFrame(scroll, fg_color="transparent")
                frame.pack(fill="x", pady=2)
                ctk.CTkLabel(frame, text=f"{img}.png:", width=130, anchor="e").pack(side="left")
                entry = ctk.CTkEntry(frame, width=200)
                entry.insert(0, name)
                entry.pack(side="left", padx=5)
                self.hero_entries[img] = entry
        
        def setup_gear_tab(self):
            tab = self.tabview.tab("⚙️ Gears")
            
            desc_frame = ctk.CTkFrame(tab, fg_color="#2b2b2b", corner_radius=8)
            desc_frame.pack(fill="x", padx=10, pady=(10, 5))
            ctk.CTkLabel(
                desc_frame, 
                text="📌 ตั้งชื่อ Gear ที่จะบันทึก\\nเมื่อบอทพบรูป gearimgX.png จะตั้งชื่อไฟล์ตามที่กำหนด", 
                font=ctk.CTkFont(size=11),
                text_color="gray",
                justify="left"
            ).pack(padx=10, pady=5)
            
            ctk.CTkLabel(tab, text="รูป → ชื่อ Gear", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=5)
            
            self.gear_entries = {}
            gear_mapping = self.cfg.get("gearname", {})
            
            scroll = ctk.CTkScrollableFrame(tab, width=480, height=300)
            scroll.pack(fill="both", expand=True, padx=10)
            
            for img, name in gear_mapping.items():
                frame = ctk.CTkFrame(scroll, fg_color="transparent")
                frame.pack(fill="x", pady=2)
                ctk.CTkLabel(frame, text=f"{img}.png:", width=130, anchor="e").pack(side="left")
                entry = ctk.CTkEntry(frame, width=200)
                entry.insert(0, name)
                entry.pack(side="left", padx=5)
                self.gear_entries[img] = entry
        
        def setup_weapon_tab(self):
            tab = self.tabview.tab("🔫 Weapons")
            ctk.CTkLabel(tab, text="เปิด/ปิด Weapon ที่ต้องการตรวจสอบ", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=10)
            
            self.weapon_vars = {}
            weapon_mapping = self.cfg.get("weaponname", {})
            
            for img, enabled in weapon_mapping.items():
                var = ctk.BooleanVar(value=enabled == "true" or enabled == True)
                self.weapon_vars[img] = var
                ctk.CTkSwitch(tab, text=img, variable=var).pack(pady=5, padx=20, anchor="w")
        
        def save_all(self):
            try:
                hero_mapping = {}
                for img, entry in self.hero_entries.items():
                    hero_mapping[img] = entry.get()
                self.cfg["HERO_MAPPING"] = hero_mapping
                
                gear_mapping = {}
                for img, entry in self.gear_entries.items():
                    gear_mapping[img] = entry.get()
                self.cfg["gearname"] = gear_mapping
                
                weapon_mapping = {}
                for img, var in self.weapon_vars.items():
                    weapon_mapping[img] = "true" if var.get() else "false"
                self.cfg["weaponname"] = weapon_mapping
                
                with open('configmain.json', 'w', encoding='utf-8') as f:
                    json.dump(self.cfg, f, indent=4, ensure_ascii=False)
                
                messagebox.showinfo("สำเร็จ", "บันทึก Ranger & Gear เรียบร้อย!")
                try:
                    global load_config
                    load_config()
                except Exception as ex:
                    print(ex)
                self.parent.log("INFO", "✅ Ranger & Gear อัพเดทแล้ว")
                self.destroy()
            except Exception as e:
                messagebox.showerror("Error", f"บันทึกไม่สำเร็จ: {e}")


    class DeviceMonitorWidget(ctk.CTkFrame):
        def __init__(self, parent, device_id, index):
            super().__init__(parent, fg_color="#383838", corner_radius=6, height=32)
            self.device_id = device_id
            self.pack_propagate(False)
            
            chk = ctk.CTkCheckBox(self, text="", width=20, height=20, checkbox_width=16, checkbox_height=16)
            chk.pack(side="left", padx=(6, 2))
            chk.select()
            
            ctk.CTkLabel(self, text=f"#{index}", font=ctk.CTkFont(size=11, weight="bold"), text_color="#ffffff", width=25).pack(side="left", padx=(0, 4))
            ctk.CTkLabel(self, text=device_id, font=ctk.CTkFont(family="Consolas", size=10), text_color="#ccc").pack(side="left", padx=(0, 6))
            
            self.lbl_status = ctk.CTkLabel(self, text="Ready", font=ctk.CTkFont(size=10, weight="bold"), text_color="#4caf50", width=60)
            self.lbl_status.pack(side="right", padx=6)
            
            ctk.CTkButton(self, text="↺", width=22, height=20, font=ctk.CTkFont(size=11, weight="bold"), fg_color="#e53935").pack(side="right", padx=2)

        def update_state(self, status=None, **kwargs):
            if status:
                color_map = {'working': "#4caf50", 'waiting': "#ff9800", 'error': "#e53935", 'idle': "#888"}
                self.lbl_status.configure(text=status.upper(), text_color=color_map.get(status, "#888"))

    class ModernBotGUI(ctk.CTk):
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
            self.hero_stats_labels = {}
            self.hero_rows = {}
            self.hero_filter_text = ""
            self.is_started = False
            
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
            toolbar = ctk.CTkFrame(self, height=40, fg_color="#333333", corner_radius=0)
            toolbar.pack(fill="x")
            toolbar.pack_propagate(False)
            
            self.lbl_status = ctk.CTkLabel(toolbar, text=f"   ● ONLINE ({len(self.devices)})", font=ctk.CTkFont(size=12, weight="bold"), text_color="#4caf50")
            self.lbl_status.pack(side="left", padx=5)

            self.btn_start = ctk.CTkButton(toolbar, text="▶ START", font=ctk.CTkFont(size=12, weight="bold"), width=80, height=24, fg_color="#e53935", hover_color="#c62828", command=self.start_bot)
            self.btn_start.pack(side="left", padx=10)
            
            self.lbl_auto_start = ctk.CTkLabel(toolbar, text="[ WAITING FOR START ]", font=ctk.CTkFont(size=10, weight="bold"), text_color="#aaaaaa")
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
            
            self.lbl_avg_time = ctk.CTkLabel(toolbar, text="Avg: -", font=ctk.CTkFont(size=12, weight="bold"), text_color="#2196f3")
            self.lbl_avg_time.pack(side="right", padx=15)

            from datetime import datetime
            start_time_str = datetime.now().strftime("%H:%M:%S")
            self.lbl_start_time = ctk.CTkLabel(toolbar, text=f"Started: {start_time_str}", font=ctk.CTkFont(size=12, weight="bold"), text_color="#aaaaaa")
            self.lbl_start_time.pack(side="right", padx=15)
            
            # 2. MAIN CONTENT
            main_frame = ctk.CTkFrame(self, fg_color="transparent")
            main_frame.pack(fill="both", expand=True, padx=6, pady=4)
            main_frame.grid_columnconfigure(0, weight=3)
            main_frame.grid_columnconfigure(1, weight=2)
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
                m = DeviceMonitorWidget(self.dev_scroll, dev, i+1)
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
            
            # 4. BOTTOM BAR
            bottom_bar = ctk.CTkFrame(self, height=32, fg_color="#333333", corner_radius=0)
            bottom_bar.pack(fill="x")
            
            base_path = os.path.dirname(os.path.abspath(__file__))
            backup_folder = os.path.join(base_path, "backup")
            heroes_folder = os.path.join(base_path, "backup-id")
            
            ctk.CTkButton(bottom_bar, text="🔌 Connect Missing", width=85, height=22, font=ctk.CTkFont(size=10), fg_color="#4caf50", command=self.connect_missing_devices).pack(side="left", padx=3, pady=4)
            ctk.CTkButton(bottom_bar, text="⚙ Config", width=70, height=22, font=ctk.CTkFont(size=10), fg_color="#555555", command=self.open_config).pack(side="left", padx=3, pady=4)
            ctk.CTkButton(bottom_bar, text="📁 Backup", width=70, height=22, font=ctk.CTkFont(size=10), fg_color="#555555", command=lambda: subprocess.Popen(f'explorer "{backup_folder}"')).pack(side="left", padx=3, pady=4)
            ctk.CTkButton(bottom_bar, text="🦸 Heroes", width=70, height=22, font=ctk.CTkFont(size=10), fg_color="#555555", command=lambda: subprocess.Popen(f'explorer "{heroes_folder}"')).pack(side="left", padx=3, pady=4)
            ctk.CTkLabel(bottom_bar, text="v3.2.0", font=ctk.CTkFont(size=10), text_color="#888888").pack(side="right", padx=8)

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
                    new_count += 1
                    self.devices.append(dev)
                    # Add to UI
                    m = DeviceMonitorWidget(self.dev_scroll, dev, len(self.devices))
                    m.pack(fill="x", pady=1)
                    self.device_monitors[dev] = m
                    
                    # Start bot thread
                    if getattr(self, 'is_started', False) and not getattr(self.args, 'no_start', False):
                        bot = RangerGearBot(dev, self.args)
                        bot.start()
                        self.bot_threads.append(bot)
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
            bot = RangerGearBot(device_id, self.args)
            bot.start()
            self.bot_threads.append(bot)
            self.log("INFO", f"🚀 Started bot on {device_id}")

        def start_bot(self):
            if getattr(self, 'is_started', False):
                self.log("WARN", "Bot is already running.")
                return
            self.is_started = True
            if hasattr(self, 'btn_start'):
                self.btn_start.configure(state="disabled", fg_color="#555555", text="⏳ RUNNING")
            self.lbl_auto_start.configure(text="[ BOT IS RUNNING ]", text_color="#4caf50")
            
            delay_sec = config.get("thread_delay", 5)
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
                            self.device_monitors[dev].update_state(status=stat.get('status'))
                    
                    hero_raw_data = ui_stats.get_hero_combo_stats()
                    hero_data = hero_raw_data.copy()
                    
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
                        # Hide "Success" label if swap_shop is enabled as per user request
                        if not config.get("swap_shop", 0):
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
                    if config.get("swap_shop", 0): to_hide.append("✅ สำเร็จ (Success)")
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
            total_filtered = 0
            for hero, row in self.hero_rows.items():
                if not self.hero_filter_text or self.hero_filter_text in hero.lower():
                    row.pack(fill="x", pady=1)
                    # Get count from label text
                    try:
                        count = int(self.hero_stats_labels[hero].cget("text"))
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
        def open_heroes(self): HeroConfigWindow(self)

# =============================================================
# Global Config
# =============================================================
# Default config (will be overridden by config files)
config = {
    "first_loop": True,
    "thread_delay": 5,
    "find_ranger": 0,
    "find_gear": 0,
    "find_all": 1,
    "custommode": 0,
    "custom": {"characters": []},
    "characters": [],
    "ranger_images": {},
    "gearname": {},
    "weaponname": {},
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
            if _ocr_reader is None:
                import easyocr
                print("[INFO] Loading EasyOCR model (first time only)...")
                _ocr_reader = easyocr.Reader(['en'], gpu=False)
                print("[OK] EasyOCR model loaded!")
    return _ocr_reader


def load_config():
    global config
    
    # 1. Load main config from ranger-gear_config.json
    main_config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranger-gear_config.json")
    if os.path.exists(main_config_file):
        try:
            with open(main_config_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                config.update(loaded)
            print(f"[CONFIG] Base Loaded: {main_config_file}")
        except Exception as e:
            print(f"[WARN] Error loading base config: {e}")

    # 2. Load UI settings from configmain.json (Post-login tasks etc.)
    ui_config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configmain.json")
    if os.path.exists(ui_config_file):
        try:
            with open(ui_config_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                config.update(loaded)
            print(f"[CONFIG] UI Settings Loaded: {ui_config_file}")
        except Exception as e:
            print(f"[WARN] Error loading UI config: {e}")


def find_adb_executable():
    global adb_path
    
    # Check common locations
    script_dir = os.path.dirname(os.path.abspath(__file__))
    adb_locations = [
        os.path.join(script_dir, "adb", "adb.exe"),
        os.path.join(script_dir, "adb", "adb"),
        "adb",
    ]
    
    # Add current working directory as another check
    adb_locations.append(os.path.join(os.getcwd(), "adb", "adb.exe"))
    
    for loc in adb_locations:
        if not loc.endswith(".exe") and sys.platform == 'win32' and not os.path.isabs(loc):
             pass # Skip simple "adb" for exists check if it's just a command
        elif os.path.exists(loc):
            print(f"[ADB] Found file at {loc}, testing...")
            try:
                result = subprocess.run(
                    [loc, "version"],
                    capture_output=True, text=True, timeout=15,
                    shell=(sys.platform == 'win32')
                )
                if result.returncode == 0:
                    adb_path = loc
                    print(f"[ADB] Verified: {adb_path}")
                    return True
            except Exception as e:
                print(f"[ADB] Error testing {loc}: {e}")
        
        # Also try running loc directly if it's a command name like "adb"
        if loc == "adb":
            try:
                result = subprocess.run(
                    [loc, "version"],
                    capture_output=True, text=True, timeout=15,
                    shell=(sys.platform == 'win32')
                )
                if result.returncode == 0:
                    adb_path = loc
                    print(f"[ADB] Verified command: {adb_path}")
                    return True
            except:
                pass
    
    # Try system PATH
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
    
    # Try MuMu emulator paths
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
            print(f"[ADB] Found MuMu ADB: {path}")
            return True
    
    return False


def connect_known_ports():
    """Auto-scan ALL emulator ports, connect everything that responds"""
    try:
        # Kill & start adb server
        subprocess.run([adb_path, "kill-server"], capture_output=True, timeout=3)
        time.sleep(0.1)
        subprocess.run([adb_path, "start-server"], capture_output=True, timeout=3)
        time.sleep(0.5)

        # สแกนพอร์ตคี่ตั้งแต่ 5555-5755 (รองรับ 100 จอ MuMu)
        ports = list(range(5555, 5756, 2))  # [5555, 5557, 5559, ..., 5755]

        print(f"\n--- [ADB] Auto-scanning {len(ports)} ports (5555-5755 odd) ---")
        
        connected = []
        
        def try_connect_port(port):
            """ยิงเชื่อมต่อทีละพอร์ต"""
            try:
                addr = f"127.0.0.1:{port}"
                result = subprocess.run(
                    [adb_path, "connect", addr],
                    capture_output=True, timeout=1, text=True
                )
                out = result.stdout.lower()
                if ("connected" in out or "already connected" in out) and "cannot" not in out:
                    return addr
            except Exception:
                pass
            return None

        # ยิงเชื่อมต่อพร้อมกัน
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            futures = {executor.submit(try_connect_port, p): p for p in ports}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    connected.append(result)
        
        if connected:
            print(f"[ADB] Port scan found {len(connected)} device(s): {', '.join(sorted(connected))}")
        else:
            print("[ADB] Port scan found no devices.")
                
        print("--- Scan Complete ---\n")
    except Exception as e:
        print(f"[ADB] Port scan error: {e}")


def get_connected_devices():
    """ดึงรายชื่อ devices ที่ online จาก adb devices (ไม่จำกัดจำนวน, กรองซ้ำ)"""
    try:
        result = subprocess.run(
            [adb_path, "devices"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")[1:]
        raw_devices = []
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                raw_devices.append(parts[0])
        
        if not raw_devices:
            return []
                
        # กรองซ้ำ: ถ้ามี emulator-5556 อยู่แล้ว ไม่ต้องเอา 127.0.0.1:5557 อีก
        emulator_adb_ports = set()  # เก็บพอร์ต ADB (คี่) ที่ emulator-xxx ครอง
        for d in raw_devices:
            if d.startswith("emulator-"):
                try:
                    console_port = int(d.replace("emulator-", ""))
                    emulator_adb_ports.add(console_port + 1)  # emulator-5556 -> ADB port 5557
                except ValueError:
                    pass
        
        final_devices = []
        seen = set()
        for d in raw_devices:
            if d in seen:
                continue
            # ถ้าเป็น 127.0.0.1:port แล้วมี emulator- ครองอยู่แล้ว -> ข้าม
            if d.startswith("127.0.0.1:"):
                try:
                    port = int(d.split(":")[1])
                    if port in emulator_adb_ports:
                        continue  # ซ้ำกับ emulator-xxxx
                except ValueError:
                    pass
            seen.add(d)
            final_devices.append(d)
        
        return final_devices
    except Exception as e:
        print(f"[ERR] get_connected_devices: {e}")
        return []


def ImgSearchADB(adb_img, find_img_path, threshold=0.95, method=cv2.TM_CCOEFF_NORMED):
    try:
        # Load template from the bot's static method to use cache if possible, 
        # but here we'll just read it normally to match mainLG.py behavior exactly
        find_img = cv2.imread(find_img_path, cv2.IMREAD_COLOR)
        if find_img is None:
            # print(f"ไม่สามารถโหลดรูปภาพ {find_img_path}")
            return []
        
        needle_w = find_img.shape[1]
        needle_h = find_img.shape[0]
        result = cv2.matchTemplate(adb_img, find_img, method)
        locations = np.where(result >= threshold)
        locations = list(zip(*locations[::-1]))
        rectangles = []
        for loc in locations:
            rect = [int(loc[0]), int(loc[1]), needle_w, needle_h]
            rectangles.append(rect)
            rectangles.append(rect)
        if len(rectangles):
            rectangles, _ = cv2.groupRectangles(rectangles, groupThreshold=1, eps=1)
        points = []
        if len(rectangles):
            for (x, y, w, h) in rectangles:
                center_x = x + int(w / 2)
                center_y = y + int(h / 2)
                points.append((center_x, center_y))
        return points
    except Exception as e:
        # print(f"เกิดข้อผิดพลาดในการค้นหารูปภาพ: {e}")
        return []

class NetworkMonitor:
    def __init__(self):
        self.last_check = time.time()
        self.check_interval = 10
        
    def check_network(self, bot, adb_img):
        current_time = time.time()
        if current_time - self.last_check >= self.check_interval:
            # Note: bot here is the RangerGearBot instance
            fixnet_pos = ImgSearchADB(adb_img, 'img/fixnet.png')
            if fixnet_pos:
                print(f"[{bot.device_id}] พบปัญหาการเชื่อมต่อ (fixnet.png)")
                bot.tap(fixnet_pos[0][0], fixnet_pos[0][1])
                time.sleep(1)
                return True
            self.last_check = current_time
        return False

def check_critical_errors(bot, adb_img, context=""):
    """
    ตรวจสอบ fixid.png, fixunkown.png, apple.png
    """
    try:
        # ตรวจสอบ fixid.png
        fixid_pos = ImgSearchADB(adb_img, 'img/fixid.png')
        if fixid_pos:
            print(f"[{bot.device_id}] ⚠️ Found fixid.png in {context}!")
            bot.backup_to_backupxml()
            bot.clear_and_restart()
            time.sleep(6)
            return "fixid"
        
        # ตรวจสอบ fixunkown.png
        fixunkown_pos = ImgSearchADB(adb_img, 'img/fixunkown.png')
        if fixunkown_pos:
            print(f"[{bot.device_id}] ⚠️ Found fixunkown.png in {context}!")
            bot.backup_to_backupxml()
            bot.clear_and_restart()
            time.sleep(6)
            return "fixunkown"
        
        # ตรวจสอบ apple.png
        apple_pos = ImgSearchADB(adb_img, 'img/apple.png')
        if apple_pos:
            print(f"[{bot.device_id}] ⚠️ Found apple.png in {context}!")
            bot.backup_failed_login()
            bot.clear_and_restart()
            time.sleep(6)
            return "apple"
            
        # ตรวจสอบ fixnet1.png / fixnet.png (ปัญหาเน็ตหลุดเด้งป๊อปอัพ)
        fixnet_pos = ImgSearchADB(adb_img, 'img/fixnet1.png') or ImgSearchADB(adb_img, 'img/fixnet.png')
        if fixnet_pos:
            print(f"[{bot.device_id}] 📶 พบปัญหาการเชื่อมต่อ (fixnet1/fixnet) ใน {context} - กำลังกด OK...")
            bot.tap(fixnet_pos[0][0], fixnet_pos[0][1])
            time.sleep(1)
            # return None เพื่อให้ลูปทำงานปกติต่อไป (แค่กดป๊อปอัพทิ้ง)
        
        return None
    except Exception as e:
        print(f"[ERROR] check_critical_errors: {e}")
        return None

def load_hero_mapping():
    try:
        # Access global config
        hero_mapping = config.get('HERO_MAPPING', {})
        if not hero_mapping:
            return {
                'heroo1.png': 'Denji',
                'heroo2.png': 'DenjiU',
                'heroo3.png': 'Power',
                'heroo4.png': 'PowerU'
            }
        
        converted_mapping = {}
        for key, value in hero_mapping.items():
            if key == 'gachahero1': converted_mapping['heroo1.png'] = value
            elif key == 'gachahero2': converted_mapping['heroo2.png'] = value
            elif key == 'gachahero3': converted_mapping['heroo3.png'] = value
            elif key == 'gachahero4': converted_mapping['heroo4.png'] = value
            else: converted_mapping[key] = value # Preserve others
        
        return converted_mapping
    except:
        return {'heroo1.png': 'Denji', 'heroo2.png': 'DenjiU', 'heroo3.png': 'Power', 'heroo4.png': 'PowerU'}

def check_hero_images(bot, adb_img):
    try:
        hero_images = ['heroo1.png', 'heroo2.png', 'heroo3.png', 'heroo4.png']
        for hero_img in hero_images:
            hero_pos = ImgSearchADB(adb_img, f'img/ranger/{hero_img}')
            if hero_pos:
                print(f"[{bot.device_id}] พบ {hero_img}")
                return True
        return False
    except: return False

def search_gachaslot_image(bot):
    """Search for gachaslot.png with swiping logic from mainLG.py"""
    max_swipes = 5
    swipe_count = 0
    while swipe_count <= max_swipes:
        bot.capture_screen()
        adb_img = bot._screen_color
        gachaslot_pos = ImgSearchADB(adb_img, 'img/gachaslot.png')
        if gachaslot_pos:
            return gachaslot_pos[0]
        if swipe_count < max_swipes:
            print(f"[{bot.device_id}] ไม่พบ gachaslot.png - เลื่อนหน้าจอครั้งที่ {swipe_count + 1}")
            bot.adb_shell("input swipe 824 240 808 109 1000")
            time.sleep(1)
            swipe_count += 1
        else: return None
    return None

def get_next_backup_id():
    filename_prefix = config.get('filename_prefix', 'conyfly')
    backup_dir = "backup-id"
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        return 1, filename_prefix
        
    existing_files = glob.glob(os.path.join(backup_dir, f"{filename_prefix}-id*_LINE_COCOS_PREF_KEY_*.xml"))
    if not existing_files: return 1, filename_prefix
        
    ids = []
    for file in existing_files:
        try:
            id_part = file.split(f"{filename_prefix}-id")[1].split("_")[0]
            if id_part.isdigit(): ids.append(int(id_part))
        except: continue
    return (max(ids) + 1 if ids else 1), filename_prefix

# =============================================================
# RangerGearBot Class - Unified Bot for Ranger + Gear
# =============================================================
class RangerGearBot(threading.Thread):
    def __init__(self, device_id, args=None):
        threading.Thread.__init__(self)
        self.device_id = device_id
        self.args = args # Store command line args
        self.daemon = True
        
        def update_gui_status(self, step, status="working"):
            ui_stats.update_device(self.device_id, {'step': step, 'status': status})
        self.update_gui_status = update_gui_status.__get__(self, RangerGearBot)
        
        # Determine which modes to run STRICTLY from configmain.json toggles
        self.do_ranger = config.get("find_ranger", 0)
        self.do_gear = (config.get("check-gear", 0) or 
                        config.get("ruby-gear200", 0) or 
                        config.get("random-gear", 0))
        
        print(f"[{self.device_id}] Mode - Ranger Scan: {self.do_ranger}, Gear Scan: {self.do_gear}")
        
        # Unique filename for this thread
        safe_dev = device_id.replace(":", "_")
        self.filename = os.path.join(tempfile.gettempdir(), f"screen-{safe_dev}.png")
        self.first_loop_done = not config.get("first_loop", True)
        self.last_activity_time = time.time()
        
        # Ranger Characters List (Always try to get from configmain first)
        if self.do_ranger:
            # Check characters in configmain first, then fallback to base config
            self.characters = config.get("characters", [])
            print(f"[{self.device_id}] Ranger mode -> searching {len(self.characters)} characters")
            
            # Auto-scan img/ranger/ folder for all png files
            self.ranger_image_mapping = config.get("ranger_images", {})
            ranger_folder = os.path.join("img", "ranger")
            self.ranger_files = []
            if os.path.exists(ranger_folder):
                for f in sorted(os.listdir(ranger_folder)):
                    if f.lower().endswith(".png"):
                        self.ranger_files.append(f"ranger/{f}")
                print(f"[{self.device_id}] Auto-loaded {len(self.ranger_files)} ranger images from img/ranger/")
        
        # Gear Config
        if self.do_gear:
            self.gear_names = config.get("gearname", {})
            self.weapon_names = config.get("weaponname", {})
            self.ocr_region = config.get("ocr_region", {"x": 463, "y": 153, "w": 397, "h": 321})
            print(f"[{self.device_id}] Gear mode -> {len(self.gear_names)} gears to check")
        
        # Store original filename for backup
        self.current_original_filename = None
        
        # Sequence Definitions (Reverted to use coordinates for checkboxes)
        self.seq1 = ['icon.png', 'apple.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png']
        self.seq2 = ['check-gusetid.png', 'check-gusetid1.png', '@check-l1.png', (932, 133), (930, 253), (926, 327), 'check-l4.png', 'check-ok1.png', 'check-ok2.png', 'check-ok3.png', 'check-ok4.png']
        
        self.adb_cmd = adb_path
        self._screen = None
        self._screen_color = None
        self._screen_raw_png = None  # raw PNG for lazy color decode
        self._template_cache = {}
        self._black_start_time = None
        
        # Post-Login Task Sequences
        self.box_seq = ['box1.png', 'box2.png', 'box3.png', 'box4.png', 'box5.png', 'box6.png', 'end_box.png']
        self.seven_day_seq = ['7day.png', '7day1.png', '7day2.png', 'fixok.png']
        self.shop_gacha_seq = ['gacha.png', 'gacha1.png', 'gacha3.png', 'fixok.png']
        self.swap_shop_seq = ['swap_shop.png', 'swap_shop1.png', 'swap_shop2.png', 'swap_shop3.png', 'swap_shop4.png', 'fixok.png']
        
        self._fixnetv3_count = 0
        self._fixokk_start = None
        self._alert2_start = None
        self._need_restart = False
        self._running = True
        self._capture_count = 0  # throttle popup checks
        
        # Start background monitor thread
        self.monitor_thread = threading.Thread(target=self._popup_monitor_loop, daemon=True)
        self.monitor_thread.start()

    def open_app(self):
        self.last_activity_time = time.time()
        """เปิดแอป LINE Rangers ด้วยคำสั่ง am start / monkey (เร็วกว่าคลิก icon.png)"""
        attempt = 0
        while attempt < 5:
            attempt += 1
            try:
                # สลับวิธีเปิด: am start กับ monkey
                if attempt % 2 == 1:
                    self.adb_run([
                        self.adb_cmd, "-s", self.device_id, "shell",
                        "am", "start", "-S", "-n",
                        "com.linecorp.LGRGS/com.linecorp.common.activity.LineActivity"
                    ], timeout=10)
                else:
                    self.adb_run([
                        self.adb_cmd, "-s", self.device_id, "shell",
                        "monkey", "-p", "com.linecorp.LGRGS",
                        "-c", "android.intent.category.LAUNCHER", "1"
                    ], timeout=10)
                
                sleep(3)
                
                # ตรวจว่าแอปยังรันอยู่ด้วย pidof
                try:
                    pid_result = subprocess.run(
                        [self.adb_cmd, "-s", self.device_id, "shell", "pidof", "com.linecorp.LGRGS"],
                        capture_output=True, text=True, timeout=5
                    )
                    pid = pid_result.stdout.strip()
                except Exception:
                    pid = ""
                
                if pid:
                    print(f"[{self.device_id}] ✓ App running (PID: {pid}) - attempt {attempt}")
                    return True
                else:
                    print(f"[{self.device_id}] ✗ App crashed/bounced! (attempt {attempt}) Retrying...")
                    sleep(2)
                    
            except Exception as e:
                print(f"[{self.device_id}] Error opening app (attempt {attempt}): {e}")
                sleep(2)
        
        print(f"[{self.device_id}] Failed to open app after 5 attempts!")
        return False

    def backup_game_data(self, hero_prefix=None):
        try:
            print(f"[{self.device_id}] กำลังสำรองข้อมูลเกม...")
            backup_dir = "backup-id"
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir)
            
            original_name = getattr(self, "current_original_filename", "unknown.xml")
            if not original_name.endswith(".xml"): original_name += ".xml"
            
            # Use hero_prefix if provided, else use config/default
            prefix = hero_prefix or config.get('filename_prefix', 'conyfly')
            dest_filename = f"{prefix}-{original_name}"
            dest_path = os.path.join(backup_dir, dest_filename)
            source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
            temp_path = f"/data/local/tmp/backup_{self.device_id.replace(':','_')}.xml"
            
            self.adb_shell(f"su -c 'cp {source_path} {temp_path}'")
            self.adb_shell(f"su -c 'chmod 666 {temp_path}'")
            res = subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", temp_path, dest_path], 
                                 capture_output=True, timeout=15)
            self.adb_shell(f"su -c 'rm {temp_path}'")
            if os.path.exists(dest_path):
                print(f"[{self.device_id}] สำรองข้อมูลสำเร็จ: {dest_path}")
                return True
            else:
                print(f"[{self.device_id}] สำรองข้อมูลล้มเหลว: {res.stderr.decode()}")
                return False
        except Exception as e:
            print(f"[{self.device_id}] เกิดข้อผิดพลาดในการสำรองข้อมูล: {e}")
            return False

    def backup_failed_game_data(self):
        try:
            print(f"[{self.device_id}] ตรวจไม่พบฮีโร่ - กำลังสำรองข้อมูลไปยัง not-found...")
            backup_dir = "not-found"
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir)
            
            original_name = getattr(self, "current_original_filename", "unknown.xml")
            if not original_name.endswith(".xml"): original_name += ".xml"
            
            dest_filename = f"FAIL-{original_name}"
            dest_path = os.path.join(backup_dir, dest_filename)
            source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
            temp_path = f"/data/local/tmp/fail_backup_{self.device_id.replace(':','_')}.xml"
            
            self.adb_shell(f"su -c 'cp {source_path} {temp_path}'")
            self.adb_shell(f"su -c 'chmod 666 {temp_path}'")
            res = subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", temp_path, dest_path], 
                                 capture_output=True, timeout=15)
            self.adb_shell(f"su -c 'rm {temp_path}'")
            if os.path.exists(dest_path):
                print(f"[{self.device_id}] สำรองข้อมูลล้มเหลวสำเร็จ: {dest_path}")
                return True
            return False
        except Exception as e:
            print(f"[{self.device_id}] Error backup failed data: {e}")
            return False

    def backup_failed_login(self):
        try:
            print(f"[{self.device_id}] ล็อกอินล้มเหลว/ติด apple - กำลังสำรองข้อมูลไปยัง login-fail...")
            filename_prefix = config.get('filename_prefix', 'conyfly')
            backup_dir = "login-fail"
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir)
            file_count = len([f for f in os.listdir(backup_dir) if f.startswith(filename_prefix)])
            next_num = file_count + 1
            source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
            dest_path = os.path.join(backup_dir, f"{filename_prefix}-loginfail{next_num}_LINE_COCOS_PREF_KEY_.xml")
            self.adb_shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
            self.adb_shell(f"su -c 'chmod 777 {source_path}'")
            subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", source_path, dest_path], 
                           capture_output=True, timeout=15)
            if os.path.exists(dest_path):
                print(f"[{self.device_id}] ย้ายไฟล์ไป login-fail สำเร็จ: {dest_path}")
                return True
            return False
        except Exception as e:
            print(f"[{self.device_id}] Error backup failed login: {e}")
            return False

    def backup_to_backupxml(self):
        try:
            if not self.current_original_filename:
                return False
            print(f"[{self.device_id}] ย้ายไฟล์ {self.current_original_filename} กลับไป backup/ เพื่อวนเข้าใหม่...")
            current_dir = os.path.dirname(os.path.abspath(__file__))
            backup_xml_dir = os.path.join(current_dir, "backup")
            source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
            dest_path = os.path.join(backup_xml_dir, self.current_original_filename)
            self.adb_shell("su -c 'chmod 777 /data/data/com.linecorp.LGRGS/shared_prefs'")
            self.adb_shell(f"su -c 'chmod 777 {source_path}'")
            subprocess.run([self.adb_cmd, "-s", self.device_id, "pull", source_path, dest_path], 
                           capture_output=True, timeout=15)
            if os.path.exists(dest_path):
                print(f"[{self.device_id}] ย้ายไฟล์กลับ backup/ สำเร็จ: {dest_path}")
                return True
            return False
        except Exception as e:
            print(f"[{self.device_id}] Error backup back to backupxml: {e}")
            return False

    def auto_trade(self):
        try:
            trade_count_target = config.get('trade_count', 0)
            if trade_count_target <= 0: return "complete"
            print(f"[{self.device_id}] Starting Auto Trade ({trade_count_target} times)")
            current_trades = 0
            while current_trades < trade_count_target:
                self.capture_screen()
                img = self._screen_color
                critical = check_critical_errors(self, img, "auto_trade")
                if critical: return critical
                # First step: buy (272, 396)
                self.tap(272, 396)
                time.sleep(1)
                # Confirm step: buy_confirm (480, 420)
                self.tap(480, 420)
                time.sleep(1.2)
                # Success step: buy_ok (480, 420)
                self.tap(480, 420)
                current_trades += 1
                time.sleep(0.5)
                print(f"[{self.device_id}] Trade {current_trades}/{trade_count_target} complete")
            return "complete"
        except Exception as e:
            print(f"[{self.device_id}] Auto trade error: {e}")
            return "error"

    def process_shopgacha(self):
        try:
            shop_gacha_enabled = config.get('shop_gacha', 0)
            if not shop_gacha_enabled: return "complete"
            print(f"[{self.device_id}] Starting Shop Gacha tasks...")
            self.process_sequence(self.shop_gacha_seq)
            return "complete"
        except Exception as e:
            print(f"[{self.device_id}] Shop gacha error: {e}")
            return "error"

    def process_swap_shopevent(self):
        print(f"[{self.device_id}] เริ่ม swap shop event")
        try:
            # Stage 1
            if check_critical_errors(self, self._screen_color, "stage1"): return "restart"
            if self.exists_in_cache("img/stopstep2.png"):
                self.clear_and_restart()
                return "stopped_by_stopstep2"
            
            # Sequence for Stage 1
            for img in ['gachaevent1.png', 'gachaevent2.png', 'gachaevent3.png']:
                pos = self._find_img_in_any_screen(self._screen_color, f"img/{img}")
                if pos:
                    self.tap(pos[0], pos[1])
                    time.sleep(1)
                    if self.exists("img/stopstep2.png"):
                        self.clear_and_restart()
                        return "stopped_by_stopstep2"

            # gachaevent4 x 20
            pos4 = self._find_img_in_any_screen(self._screen_color, "img/gachaevent4.png")
            if pos4:
                for _ in range(20):
                    self.tap(pos4[0], pos4[1])
                    time.sleep(0.3)
            
            # Hero event scan
            if check_hero_images(self, self._screen_color):
                self.backup_game_data()

            # Stage 2 Loop
            step2_start = time.time()
            while time.time() - step2_start < 300:
                self.capture_screen()
                img = self._screen_color
                if self.exists_in_cache("img/stopstep2.png"): break
                
                # Random swap_shopgachaevent check
                if random.random() < 0.2: # 20% chance per capture
                    pos_ev = self._find_img_in_any_screen(img, "img/swap_shopgachaevent.png")
                    if pos_ev: self.tap(pos_ev[0], pos_ev[1])

                # Sequence 5, 6, 3
                for ev_img in ['gachaevent5.png', 'gachaevent6.png', 'gachaevent3.png']:
                    pos_ev = self._find_img_in_any_screen(img, f"img/{ev_img}")
                    if pos_ev:
                        self.tap(pos_ev[0], pos_ev[1])
                        time.sleep(1)
            
            # Stage 3
            print(f"[{self.device_id}] Stage 3 swap_shopevent")
            for step_img in ['step3ok.png', 'step3skip.png']:
                self.click(f"img/{step_img}")
                time.sleep(1)
            
            # Step 3 Loop
            all_tiket = config.get('all_tiket', 0)
            step3_start = time.time()
            while time.time() - step3_start < 300:
                self.capture_screen()
                if self.exists_in_cache("img/stopstep2.png"): break
                for loop_img in ['step3loop1.png', 'step3loop2.png']:
                    if self.exists_in_cache(f"img/{loop_img}"):
                        self.click(f"img/{loop_img}")
                        time.sleep(1)
            
            if all_tiket == 0:
                self.clear_and_restart()
            return "complete"
        except Exception as e:
            print(f"[{self.device_id}] Swap shopevent error: {e}")
            return "error"

    @classmethod
    def _find_img_in_any_screen(cls, screen_img, template_path, similarity=0.9):
        """Helper for ImgSearchADB to use the bot's template cache on arbitrary images"""
        if screen_img is None: return None
        if len(screen_img.shape) == 3:
            gray = cv2.cvtColor(screen_img, cv2.COLOR_BGR2GRAY)
        else:
            gray = screen_img
            
        tmpl = cls._get_template(template_path)
        if tmpl is None: return None
        try:
            result = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
            loc = np.where(result >= similarity)
            if len(loc[0]) > 0:
                y, x = loc[0][0], loc[1][0]
                h, w = tmpl.shape
                return (x + w // 2, y + h // 2)
        except: pass
        return None

    def process_swap_shop(self):
        """Standard process_swap_shop logic integrated from user snippet"""
        device = self # map 'device' to 'self' for snippet compatibility
        network_monitor = NetworkMonitor()

        print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] เริ่ม swap shop - Device: {device.device_id}")
        
        # โหลด config สำหรับตรวจสอบ all-in mode และ max-gacha
        try:
            all_in_mode = config.get('all-in', 0)
            max_gacha = config.get('max-gacha', 0)
            swap_shopevent_enabled = config.get('swap_shopevent', 0)
            
            if all_in_mode:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] โหมด All-In เปิดใช้งาน - ไม่ตรวจสอบ gachaout.png")
            else:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] โหมดปกติ - ตรวจสอบ gachaout.png")
            
            if max_gacha > 0:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] กำหนดจำนวนการสุ่มสูงสุด: {max_gacha} ครั้ง")
            else:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ไม่จำกัดจำนวนการสุ่ม")
        except Exception as e:
            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Error loading config: {e} - ใช้โหมดปกติ")
            all_in_mode = 0
            max_gacha = 0
            swap_shopevent_enabled = 0
        
        found_initial_swap_shop = False
        checked_waitgacha = False
        running = True
        first_sequence_position = 0
        second_sequence_position = 0
        gacha_count = 0
        
        last_click_position = None
        last_image_hash = None
        last_image_time = time.time()
        
        stopgacha4_last_seen = None
        stopgacha4_last_position = None
        gachafix_last_seen = None
        
        stopgacha4_clicked = False
        gacha3_start_time = None
        gacha3_timeout = 1.5 
        
        last_gacha1_check = time.time()
        gacha1_check_interval = 15 
        current_image_start_time = time.time()
        sequence_timeout = 2.0 
        
        def check_gachaout_after_click(timeout=3):
            if all_in_mode: return False
            start_time = time.time()
            gachaout_found_time = None
            while time.time() - start_time < timeout:
                try:
                    device.capture_screen()
                    adb_img = device._screen_color
                    gachaout_pos = ImgSearchADB(adb_img, 'img/gachaout.png')
                    if gachaout_pos:
                        if gachaout_found_time is None:
                            gachaout_found_time = time.time()
                            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] พบ gachaout.png หลังกดปุ่ม - เริ่มนับเวลา")
                        elapsed = time.time() - gachaout_found_time
                        if elapsed >= 3:
                            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] gachaout.png ค้างครบ 3 วินาที - clear app")
                            device.clear_and_restart()
                            time.sleep(6)
                            return True
                    else:
                        if gachaout_found_time is not None:
                            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] gachaout.png หายไป - รีเซ็ต")
                            gachaout_found_time = None
                    time.sleep(0.8)
                except Exception as e:
                    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Error check gachaout: {e}")
                    time.sleep(0.8)
            return False
        
        def priority_check_gachaout(action_name, timeout=8):
            nonlocal stopgacha4_clicked
            if all_in_mode or not stopgacha4_clicked: return False
            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 🔎 [PRIORITY CHECK] ก่อน {action_name} - เช็คลอยๆ {timeout} วิ...")
            check_start = time.time()
            check_count = 0
            while time.time() - check_start < timeout:
                try:
                    remaining = timeout - (time.time() - check_start)
                    check_count += 1
                    device.capture_screen()
                    img = device._screen_color
                    gachaout_pos = ImgSearchADB(img, 'img/gachaout.png') or ImgSearchADB(img, 'img/gachaout1.png')
                    if gachaout_pos:
                        print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ❌ พบ gachaout.png ก่อน {action_name} - จบ swap_shop ทันที! (ไม่ใช้เพชร)")
                        ui_stats.update_hero("สุ่มไม่ได้")
                        device.clear_and_restart()
                        time.sleep(6)
                        return True
                except Exception as e:
                    pass
            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ✅ [PRIORITY CHECK] ผ่าน - ไม่พบ gachaout.png ({timeout} วิ)")
            return False
        
        def safe_tap(x, y, image_name, delay_before=0, delay_after=0, check_gachaout_time=0.3):
            if delay_before > 0:
                time.sleep(delay_before)
            
            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ⚡ กดปุ่ม: {image_name}")
            device.tap(x, y)
            
            if delay_after > 0:
                time.sleep(delay_after)
            
            if not all_in_mode and check_gachaout_time > 0:
                check_start = time.time()
                while time.time() - check_start < check_gachaout_time:
                    try:
                        device.capture_screen()
                        img = device._screen_color
                        gachaout_pos = ImgSearchADB(img, 'img/gachaout.png') or ImgSearchADB(img, 'img/gachaout1.png')
                        if gachaout_pos:
                            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ❌ [SAFE TAP] พบ gachaout.png หลังกด {image_name}! จบ swap_shop ทันที!")
                            ui_stats.update_hero("สุ่มไม่ได้")
                            device.clear_and_restart()
                            time.sleep(6)
                            return "gachaout_found"
                        time.sleep(0.1)
                    except Exception as e:
                        time.sleep(0.1)

            return "ok"
        
        def check_and_count_swapgacha1():
            nonlocal gacha_count
            if max_gacha <= 0: return False
            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] รอ 3 วินาทีก่อนตรวจสอบ swapgacha1.png")
            time.sleep(3)
            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] เริ่มตรวจสอบ swapgacha1.png เป็นเวลา 3 วินาที")
            check_start_time = time.time()
            swapgacha1_found = False
            while time.time() - check_start_time < 3:
                try:
                    device.capture_screen()
                    check_img = device._screen_color
                    swapgacha1_pos = ImgSearchADB(check_img, 'img/swapgacha1.png')
                    if swapgacha1_pos:
                        if not swapgacha1_found:
                            gacha_count += 1
                            swapgacha1_found = True
                            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] พบ swapgacha1.png - นับ poin ครั้งที่ {gacha_count}/{max_gacha}")
                            if gacha_count >= max_gacha:
                                return "complete_gacha"
                            break
                    time.sleep(0.2)
                except Exception as e:
                    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Error checking swapgacha1.png: {e}")
                    break
            return False
        
        def check_gacha1(adb_img):
            try:
                gacha1_pos = ImgSearchADB(adb_img, 'img/gacha1.png')
                if gacha1_pos:
                    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] กด gacha1")
                    device.tap(gacha1_pos[0][0], gacha1_pos[0][1])
                    time.sleep(1.5)
                    if check_gachaout_after_click(): return "random-Fail"
                    return True
                return False
            except Exception as e:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Error gacha1: {e}")
                return False
        
        def check_fixbuggacha(adb_img):
            try:
                fixbuggacha_pos = ImgSearchADB(adb_img, 'img/fixbuggacha.png')
                if fixbuggacha_pos:
                    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] กด fixbuggacha")
                    device.tap(fixbuggacha_pos[0][0], fixbuggacha_pos[0][1])
                    time.sleep(1.5)
                    if check_gachaout_after_click(): return "random-Fail"
                    return True
                return False
            except Exception as e:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Error fixbuggacha: {e}")
                return False
        
        def check_hero_images(adb_img):
            hero_images = ['heroo1.png', 'heroo2.png', 'heroo3.png', 'heroo4.png']
            for hero_img in hero_images:
                hero_pos = ImgSearchADB(adb_img, f'img/ranger/{hero_img}')
                if hero_pos:
                    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] พบ {hero_img}")
                    return hero_img
            return None

        def get_image_hash(adb_img):
            return hashlib.md5(adb_img.tobytes()).hexdigest()

        def get_channel_position():
            try:
                channels_img_enabled = config.get('channels_img', 0)
                if channels_img_enabled == 1:
                    return search_gachaslot_image(device)
                else:
                    selected_channel = config.get('channel', 'ch2')
                    if 'channels' not in config or selected_channel not in config['channels']: return None
                    channel_pos = config['channels'][selected_channel]
                    if not isinstance(channel_pos, list) or len(channel_pos) != 2: return None
                    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ช่อง: {selected_channel}")
                    if selected_channel in ['ch4', 'ch5']:
                        for _ in range(5):
                            device.swipe(852, 316, 855, 116, 600)
                            time.sleep(0.2)
                    return channel_pos
            except Exception as e:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Error config: {e}")
                return None

        device.capture_screen()
        adb_img = device._screen_color
        event_pos = ImgSearchADB(adb_img, 'img/event.png')
        if event_pos:
            print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] กด event")
            device.tap(event_pos[0][0], event_pos[0][1])
            last_click_position = event_pos[0]
            time.sleep(2)
            if check_gachaout_after_click(): return "random-Fail"
            time.sleep(1)
        
        while running:
            try:
                device.capture_screen()
                adb_img = device._screen_color
                current_time = time.time()
                critical_error = check_critical_errors(device, adb_img, "process_swap_shop")
                if critical_error: return critical_error
                
                # Check for kaibyswap_shop.png
                kaibyswap_pos = ImgSearchADB(adb_img, 'img/kaibyswap_shop.png')
                if kaibyswap_pos:
                    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ พบ kaibyswap_shop.png - ส่งไปห้องไก่บี้")
                    device.clear_and_restart()
                    time.sleep(6)
                    return "kaiby"
                
                if not all_in_mode:
                    gachaout_priority_pos = ImgSearchADB(adb_img, 'img/gachaout.png') or ImgSearchADB(adb_img, 'img/gachaout1.png')
                    if gachaout_priority_pos:
                        print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ❌ พบ gachaout.png - จบ swap_shop")
                        ui_stats.update_hero("สุ่มไม่ได้")
                        device.clear_and_restart()
                        time.sleep(6)
                        return "random-Fail"
                
                # CONTINUOUS CHECK removed to let outer loop handle gachaout natively (0 delay)

                if network_monitor.check_network(device, adb_img): continue
                fixunkown_pos = ImgSearchADB(adb_img, 'img/fixunkown.png')
                if fixunkown_pos:
                    device.tap(477, 349)
                    time.sleep(1.5)
                    if check_gachaout_after_click(): return "random-Fail"
                    continue
                check_result = check_fixbuggacha(adb_img)
                if check_result == "restart": return "restart"
                elif check_result:
                    last_click_position = None
                    continue
                stopgacha7_pos = ImgSearchADB(adb_img, 'img/stopgacha7.png')
                if stopgacha7_pos:
                    found_hero = check_hero_images(adb_img)
                    if not found_hero: 
                        device.backup_failed_game_data()
                        ui_stats.update_hero("สุ่มไม่ได้")
                    else:
                        hero_key = found_hero.replace(".png", "").replace("heroo", "gachahero")
                        display_name = config.get("HERO_MAPPING", {}).get(hero_key, found_hero)
                        ui_stats.update_hero(display_name)
                        device.backup_game_data(display_name)
                    device.clear_and_restart()
                    time.sleep(2)
                    return "random-Fail"
                gacha3_pos = ImgSearchADB(adb_img, 'img/gacha3.png')
                if gacha3_pos:
                    if gacha3_start_time is None: gacha3_start_time = current_time
                    else:
                        if current_time - gacha3_start_time >= 0.1:
                            stopgachaok_pos = ImgSearchADB(adb_img, 'img/stopgachaok.png')
                            if stopgachaok_pos:
                                device.tap(480, 353)
                                gachaout_check_start = time.time()
                                found_gachaout = False
                                while time.time() - gachaout_check_start < 5:
                                    try:
                                        device.capture_screen()
                                        if ImgSearchADB(device._screen_color, 'img/gachaout.png'):
                                            found_gachaout = True
                                            break
                                        time.sleep(0.5)
                                    except: time.sleep(0.5)
                                if found_gachaout:
                                    device.clear_and_restart()
                                    time.sleep(6)
                                    return "random-Fail"
                            gacha3_start_time = None
                            continue
                else: gacha3_start_time = None
                if current_time - last_gacha1_check >= gacha1_check_interval:
                    check_result = check_gacha1(adb_img)
                    if check_result == "restart": return "restart"
                    last_gacha1_check = current_time
                found_hero = check_hero_images(adb_img)
                if found_hero:
                    hero_key = found_hero.replace(".png", "").replace("heroo", "gachahero")
                    display_name = config.get("HERO_MAPPING", {}).get(hero_key, found_hero)
                    if device.backup_game_data(display_name):
                        ui_stats.update_hero(display_name)
                        device.clear_and_restart()
                        time.sleep(2)
                        return "backup_complete"
                    else:
                        time.sleep(1)
                        continue
                stopgachaok_pos = ImgSearchADB(adb_img, 'img/stopgachaok.png')
                if stopgachaok_pos:
                    device.tap(480, 353)
                    time.sleep(2)
                    if check_gachaout_after_click(timeout=5): return "random-Fail"
                    continue
                stopgacha4_pos = ImgSearchADB(adb_img, 'img/stopgacha4.png')
                if stopgacha4_pos:
                    stopgacha4_clicked = True
                    stopgacha4_last_position = stopgacha4_pos[0]
                    stopgacha4_last_seen = current_time
                    if safe_tap(stopgacha4_pos[0][0], stopgacha4_pos[0][1], "stopgacha4 (1)", 0, 0, 0) == "gachaout_found": return "complete"
                    if safe_tap(stopgacha4_pos[0][0], stopgacha4_pos[0][1], "stopgacha4 (2)", 0, 0, 0.3) == "gachaout_found": return "complete"
                    if check_and_count_swapgacha1() == "complete_gacha":
                        device.clear_and_restart()
                        time.sleep(2)
                        return "random-Fail"
                    continue
                gachafix_pos = ImgSearchADB(adb_img, 'img/gachafix.png')
                if gachafix_pos:
                    if priority_check_gachaout("stopgacha6", 0.1): return "random-Fail"
                    stopgacha6_pos = ImgSearchADB(adb_img, 'img/stopgacha6.png')
                    if stopgacha6_pos:
                        last_click_position = stopgacha6_pos[0]
                        if safe_tap(stopgacha6_pos[0][0], stopgacha6_pos[0][1], "stopgacha6 (1)", 0, 0, 0) == "gachaout_found": return "random-Fail"
                        if safe_tap(stopgacha6_pos[0][0], stopgacha6_pos[0][1], "stopgacha6 (2)", 0, 0, 0.3) == "gachaout_found": return "random-Fail"
                        if check_and_count_swapgacha1() == "complete_gacha":
                            ui_stats.update_hero("สุ่มไม่ได้")
                            device.backup_failed_game_data()
                            device.clear_and_restart()
                            time.sleep(2)
                            return "random-Fail"
                current_hash = get_image_hash(adb_img)
                if current_hash == last_image_hash:
                    if current_time - last_image_time >= 1800:
                        if last_click_position:
                            device.tap(last_click_position[0], last_click_position[1])
                            if check_gachaout_after_click(): return "random-Fail"
                        last_image_time = current_time
                else:
                    last_image_hash = current_hash
                    last_image_time = current_time
                for stop_img in ['stopgacha5.png', 'stopgacha7.png', 'stopgacha8.png']:
                    if ImgSearchADB(adb_img, f'img/{stop_img}'):
                        found_hero = check_hero_images(adb_img)
                        if not found_hero: 
                            device.backup_failed_game_data()
                            ui_stats.update_hero("สุ่มไม่ได้")
                        else:
                            hero_key = found_hero.replace(".png", "").replace("heroo", "gachahero")
                            display_name = config.get("HERO_MAPPING", {}).get(hero_key, found_hero)
                            ui_stats.update_hero(display_name)
                        device.clear_and_restart()
                        time.sleep(2)
                        return "random-Fail"
                if not found_initial_swap_shop:
                    swap_shop_pos = ImgSearchADB(adb_img, 'img/gacha.png')
                    if swap_shop_pos:
                        device.tap(swap_shop_pos[0][0], swap_shop_pos[0][1])
                        last_click_position = swap_shop_pos[0]
                        found_initial_swap_shop = True
                        time.sleep(2)
                        if check_gachaout_after_click(): return "random-Fail"
                        start_time = time.time()
                        found_waitgacha = False
                        while True:
                            try:
                                device.capture_screen()
                                if not found_waitgacha:
                                    if ImgSearchADB(device._screen_color, 'img/waitgacha.png'):
                                        found_waitgacha = True
                                        start_time = time.time()
                                if found_waitgacha:
                                    fixnewgacha_pos = ImgSearchADB(device._screen_color, 'img/fixnewgacha.png')
                                    if fixnewgacha_pos:
                                        device.tap(476, 394)
                                        checked_waitgacha = True
                                        if check_gachaout_after_click(): return "random-Fail"
                                        break
                                    if time.time() - start_time > 10:
                                        checked_waitgacha = True
                                        break
                                time.sleep(0.5)
                            except: continue
                        channel_pos = get_channel_position()
                        if channel_pos and len(channel_pos) == 2:
                            device.tap(channel_pos[0], channel_pos[1])
                            if check_gachaout_after_click(): return "random-Fail"
                            if swap_shopevent_enabled: return "swap_shopevent"
                        continue
                if not checked_waitgacha:
                    if ImgSearchADB(adb_img, 'img/waitgacha.png'):
                        checked_waitgacha = True
                        time.sleep(1.5)
                        continue
                if first_sequence_position < 3:
                    purchase_sequence = ['stopgacha.png', 'stopgacha1.png', 'stopgacha2.png']
                    current_img = purchase_sequence[first_sequence_position]
                    pos = ImgSearchADB(adb_img, f'img/{current_img}')
                    if pos:
                        last_click_position = pos[0]
                        first_sequence_position += 1
                        if safe_tap(pos[0][0], pos[0][1], f"{current_img}", 0, 0, 0.3) == "gachaout_found": return "random-Fail"
                        if check_and_count_swapgacha1() == "complete_gacha":
                            ui_stats.update_hero("สุ่มไม่ได้")
                            device.clear_and_restart()
                            time.sleep(2)
                            return "random-Fail"
                        if current_img == 'stopgacha2.png': first_sequence_position = 3
                        continue
                if first_sequence_position >= 3:
                    second_sequence = ['stopgacha4.png', 'stopgacha6.png', 'stopgacha2.png']
                    current_img = second_sequence[second_sequence_position]
                    if current_time - current_image_start_time >= sequence_timeout:
                        second_sequence_position = (second_sequence_position + 1) % len(second_sequence)
                        current_image_start_time = current_time
                        continue
                    pos = ImgSearchADB(adb_img, f'img/{current_img}')
                    if pos:
                        last_click_position = pos[0]
                        second_sequence_position = (second_sequence_position + 1) % len(second_sequence)
                        current_image_start_time = current_time
                        if safe_tap(pos[0][0], pos[0][1], f"{current_img}", 0, 0, 0.3) == "gachaout_found": return "random-Fail"
                        if check_and_count_swapgacha1() == "complete_gacha":
                            device.clear_and_restart()
                            time.sleep(2)
                            return "random-Fail"
                if time.time() % 300 < 1: gc.collect()
            except Exception as e:
                print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Error: {e}")
                time.sleep(2)
        return "complete"


    def run(self):
        try:
            print(f"[{self.device_id}] RangerGear Bot Thread Started", flush=True)
            
            while True:
                # 0. Reload Config
                load_config()
                # Strict Toggles from configmain.json
                self.do_ranger = config.get("find_ranger", 0)
                self.do_gear = (config.get("check-gear", 0) or 
                                config.get("ruby-gear200", 0) or 
                                config.get("random-gear", 0))

                # 1. Look for next available file (Atomic Locking)
                xml_file = self._get_next_available_file()
                
                if not xml_file:
                    self.update_gui_status("Waiting for files", "waiting")
                    # Log only once every 60 seconds to avoid spam
                    if not hasattr(self, '_last_wait_log') or time.time() - self._last_wait_log > 60:
                        print(f"[{self.device_id}] File queue empty. Waiting for new files...")
                        self._last_wait_log = time.time()
                    sleep(10)
                    continue
                
                # Reset wait log once we get a file
                self._last_wait_log = 0

                try:
                    # Store original filename
                    self.current_original_filename = os.path.basename(xml_file)
                    
                    # 1. Check First Loop Process Toggle
                    current_first_loop_enabled = config.get("first_loop", True)
                    if current_first_loop_enabled and not self.first_loop_done:
                        self.update_gui_status("First Loop", "working")
                        res = self.first_loop_process()
                        if res == "complete":
                            self.first_loop_done = True
                        elif res == "restart":
                            # Cleanup lock if we need to restart the whole login
                            self._release_file_lock(xml_file)
                            sleep(2)
                            continue
                        elif res == "failed":
                            # Apple refresh limit reached -> move to login-failed and skip to next ID
                            print(f"[{self.device_id}] First loop FAILED (apple limit). Moving to login-failed and next ID...")
                            self.handle_failure(xml_file)
                            ui_stats.update(fail=ui_stats.fail_count + 1)
                            self.update_gui_status("Apple Failed", "error")
                            self._release_file_lock(xml_file)
                            self.first_loop_done = False
                            sleep(2)
                            continue
                    else:
                        self.first_loop_done = True
                    
                    print(f"[{self.device_id}] Processing file: {self.current_original_filename}")
                    self.update_gui_status(f"Injecting: {self.current_original_filename}")

                    # 2. Inject
                    injected_file = self.inject_file(xml_file)
                    
                    if injected_file:
                        # 3. Login
                        self.update_gui_status("Logging in...")
                        login_start_time = time.time()
                        try:
                            status = self.main_login(injected_file)
                        except RestartTimeoutError:
                            status = "timeout"
                            print(f"[{self.device_id}] Caught 500s Timeout!")
                            self.clear_and_restart()
                        
                        if status == "success":
                            ui_stats.record_login_time(time.time() - login_start_time)
                            self.handle_success(xml_file)
                            ui_stats.update(success=ui_stats.success_count + 1, processed=ui_stats.processed_files + 1)
                            self.update_gui_status("Completed", "idle")
                        elif status == "kaiby":
                            self.handle_kaiby(xml_file)
                            ui_stats.update_hero("❌ ไก่บี้")
                            self.update_gui_status("Kaiby Detected", "error")
                            self.first_loop_done = False
                        elif status == "random-Fail":
                            self.handle_random_fail(xml_file)
                            ui_stats.update(random_fail=ui_stats.random_fail_count + 1)
                            self.update_gui_status("Random/Gacha Failed", "error")
                            self.first_loop_done = False
                        elif status == "failed":
                            self.handle_failure(xml_file)
                            ui_stats.update(fail=ui_stats.fail_count + 1)
                            self.update_gui_status("Failed", "error")
                            self.first_loop_done = False
                        else:
                            print(f"[{self.device_id}] Status: {status}. Moving to next.")
                            self.handle_failure(xml_file)
                            ui_stats.update(fail=ui_stats.fail_count + 1)
                            self.update_gui_status(f"Error: {status}", "error")
                    else:
                        print(f"[{self.device_id}] Injection failed for {xml_file}")
                        self.handle_dead_file(xml_file) # Move to failed if we can't even inject
                        ui_stats.update(fail=ui_stats.fail_count + 1)
                        self.update_gui_status("Inject Failed", "error")
                    
                    # Always ensure lock is removed after processing (handle_success/failure moves the file)
                    self._release_file_lock(xml_file)
                    
                except Exception as e:
                    print(f"[{self.device_id}] Critical Error with {xml_file}: {e}")
                    self._release_file_lock(xml_file)
                    sleep(5)
        except Exception as e:
            print(f"[{self.device_id}] Thread Crash: {e}", flush=True)

    def _get_lock_path(self, xml_file):
        """Get lock file path in temp directory (ไม่รก backup folder)"""
        lock_dir = os.path.join(tempfile.gettempdir(), "ranger-locks")
        if not os.path.exists(lock_dir):
            os.makedirs(lock_dir, exist_ok=True)
        lock_name = os.path.basename(xml_file) + ".lock"
        return os.path.join(lock_dir, lock_name)

    def _get_next_available_file(self):
        """Finds next .xml file in backup/ and attempts to lock it atomically."""
        source_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
        if not os.path.exists(source_folder): return None
        
        files = [os.path.join(source_folder, f) for f in os.listdir(source_folder) if f.lower().endswith(".xml")]
        # Shuffle files so multiple processes don't hit the exact same order
        import random
        random.shuffle(files)
        
        for xml_file in files:
            lock_file = self._get_lock_path(xml_file)
            
            # 1. Clean stale locks (> 30 mins)
            if os.path.exists(lock_file):
                if time.time() - os.path.getmtime(lock_file) > 1800:
                    try: os.remove(lock_file)
                    except: pass
                else: continue
            
            # 2. Try Atomic Lock (O_CREAT | O_EXCL)
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w') as f:
                    f.write(self.device_id)
                return xml_file
            except FileExistsError:
                continue
            except Exception as e:
                print(f"[LOCK] Error creating lock for {xml_file}: {e}")
                continue
                
        return None

    def _release_file_lock(self, xml_file):
        lock_file = self._get_lock_path(xml_file)
        if os.path.exists(lock_file):
            try: os.remove(lock_file)
            except: pass

    def handle_dead_file(self, file_path):
        """Move file that failed injection or has other issues"""
        dst_dir = "login-failed"
        if not os.path.exists(dst_dir): os.makedirs(dst_dir)
        base = os.path.basename(file_path)
        try: shutil.move(file_path, os.path.join(dst_dir, base))
        except: pass

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
        
        print(f"[{self.device_id}] Login FAILED. Pulling file from device for debug...")
        
        # Pull the current file from the device to see its state
        src_remote = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        temp_remote = f"/data/local/tmp/failed_pref_{self.device_id.replace(':','_')}.xml"
        
        try:
            self.adb_shell(f"su -c 'cp {src_remote} {temp_remote}'")
            self.adb_shell(f"su -c 'chmod 666 {temp_remote}'")
            self.adb_run([self.adb_cmd, "-s", self.device_id, "pull", temp_remote, dst])
            print(f"[{self.device_id}] Saved failed session file to {dst}")
        except Exception as e:
            print(f"[{self.device_id}] Failed to pull remote file: {e}")
            # Fallback: move the original local file
            try:
                if os.path.exists(file_path):
                    shutil.move(file_path, dst)
            except: pass

        # Clean up local backup file if it still exists
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except: pass
        
        # Clear app and shared prefs to ensure device is clean
        print(f"[{self.device_id}] Clearing app data after failure...")
        self.clear_specific_shared_prefs()
        self.clear_and_restart()


    def handle_random_fail(self, file_path):
        """Handle gacha/swap_shop failure by moving file to not-found/ folder"""
        dst_dir = "not-found"
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir)
        base = os.path.basename(file_path)
        dst = os.path.join(dst_dir, base)
        
        print(f"[{self.device_id}] RANDOM/GACHA FAILED. Pulling file from device and moving to not-found...")
        
        src_remote = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        temp_remote = f"/data/local/tmp/random_fail_pref_{self.device_id.replace(':','_')}.xml"
        
        try:
            self.adb_shell(f"su -c 'cp {src_remote} {temp_remote}'")
            self.adb_shell(f"su -c 'chmod 666 {temp_remote}'")
            self.adb_run([self.adb_cmd, "-s", self.device_id, "pull", temp_remote, dst])
            print(f"[{self.device_id}] Saved random-fail file to {dst}")
        except Exception as e:
            print(f"[{self.device_id}] Failed to pull remote file for random-fail: {e}")
            try:
                if os.path.exists(file_path):
                    shutil.move(file_path, dst)
            except: pass

        # Clean up local backup file
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except: pass

    def handle_kaiby(self, file_path):
        """Handle kaiby error by moving file to kaiby/ folder and clearing app"""
        dst_dir = "kaiby"
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir)
        base = os.path.basename(file_path)
        dst = os.path.join(dst_dir, base)
        
        print(f"[{self.device_id}] KAIBY detected. Moving file to {dst_dir}/")
        
        # Clear app immediately
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(1)
        
        try:
            if os.path.exists(file_path):
                shutil.move(file_path, dst)
                print(f"[{self.device_id}] ✓ Moved to {dst_dir}: {base}")
        except Exception as e:
            print(f"[{self.device_id}] Kaiby move error: {e}")

    # =========================================================
    # Screen & Image Methods  
    # =========================================================
    @classmethod
    def _get_template(cls, template_path):
        if not hasattr(cls, '_template_cache_cls'):
            cls._template_cache_cls = {}
        
        if template_path not in cls._template_cache_cls:
            # Ensure path is absolute relative to script dir
            if not os.path.isabs(template_path):
                script_dir = os.path.dirname(os.path.abspath(__file__))
                full_path = os.path.join(script_dir, template_path)
            else:
                full_path = template_path
                
            # Convert forward slashes to backward slashes for Windows compatibility
            full_path = os.path.normpath(full_path)
            
            if not os.path.exists(full_path):
                print(f"[WARN] Image file not found: {full_path}")
                cls._template_cache_cls[template_path] = None
                return None
                
            tmpl = cv2.imread(full_path, 0)
            if tmpl is None:
                print(f"[WARN] Failed to read image (integrity check): {full_path}")
            cls._template_cache_cls[template_path] = tmpl
            
        return cls._template_cache_cls[template_path]

    def adb_run(self, args, timeout=10, **kwargs):
        if 'creationflags' not in kwargs and os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)

    def adb_shell(self, shell_cmd, timeout=10):
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(
            [self.adb_cmd, "-s", self.device_id, "shell", shell_cmd],
            capture_output=True, timeout=timeout, **kwargs)

    def capture_screen(self):
        """Capture screen and load into RAM (optimized: lazy color decode)"""
        if getattr(self, "last_activity_time", 0) and (time.time() - self.last_activity_time) > 500:
            print(f"[{self.device_id}] TIMEOUT: Inactive for 500s. Restarting bot sequence.")
            self.last_activity_time = time.time()
            raise RestartTimeoutError("500s Timeout")
        try:
            kwargs = {}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                [self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10, **kwargs
            )
            if result.returncode == 0 and len(result.stdout) > 100:
                img_array = np.frombuffer(result.stdout, np.uint8)
                self._screen = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
                self._screen_raw_png = result.stdout  # save raw for lazy color decode
                self._screen_color = None  # reset - decoded on demand via get_screen_color()
            else:
                with open(self.filename, "wb") as f:
                    f.write(result.stdout)
                self._screen = cv2.imread(self.filename, 0)
                self._screen_raw_png = None
                self._screen_color = cv2.imread(self.filename, cv2.IMREAD_COLOR)

            # Popup check every 3rd capture to reduce CPU (background thread also monitors)
            self._capture_count += 1
            if self._capture_count % 3 == 0:
                if not getattr(self, "_in_popup_check", False):
                    self._in_popup_check = True
                    try:
                        self.check_floating_popups()
                    except Exception as e:
                        print(f"[{self.device_id}] Popup check error: {e}")
                    self._in_popup_check = False
                
        except RestartTimeoutError:
            raise
        except Exception as e:
            print(f"[{self.device_id}] Capture error: {e}")
            if hasattr(self, "_in_popup_check"):
                self._in_popup_check = False

    def get_screen_color(self):
        """Lazy-load color screen (only decode when actually needed)"""
        if self._screen_color is None and self._screen_raw_png is not None:
            img_array = np.frombuffer(self._screen_raw_png, np.uint8)
            self._screen_color = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        return self._screen_color

    def _find_in_screen(self, template_path, similarity=0.95):
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

    def find(self, template_path, similarity=0.95):
        """Capture + find"""
        self.capture_screen()
        return self._find_in_screen(template_path, similarity)

    def exists(self, template_path, similarity=0.95):
        return self.find(template_path, similarity) is not None

    def exists_in_cache(self, template_path, similarity=0.95):
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

    def click(self, PSMRL, similarity=0.95):
        self.last_activity_time = time.time()
        target = None
        if isinstance(PSMRL, str):
            if os.path.exists(PSMRL):
                target = self._find_in_screen(PSMRL, similarity)
                if target is None:
                    print(f"[{self.device_id}] Template not found: {PSMRL}")
        elif isinstance(PSMRL, tuple):
            target = PSMRL
            
        if target:
            x, y = target
            self.tap(x, y) # Use the improved tap method
            return True
        return False
    
    def tap(self, x, y):
        self.last_activity_time = time.time()
        """Direct tap without image search"""
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "input", "tap", 
                     str(x), str(y)])
        
    def type_text(self, text):
        self.last_activity_time = time.time()
        """Type text via ADB (for search box) - clears it first to avoid double typing"""
        # 1. Clear text (Move to end then send backspaces)
        self.adb_shell("input keyevent 123") # MOVE_END
        for _ in range(3):
            self.adb_shell("input keyevent 67 67 67 67 67 67 67 67 67 67") # 10 backspaces at once

        # 2. Type new text
        escaped = text.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "input", "text", escaped])
        sleep(0.5) # Wait for UI to process text input

    def _popup_monitor_loop(self):
        """Background thread to monitor fixnetv3.png - reuses main thread's screen to save CPU"""
        while self._running:
            try:
                mon_screen = self._screen
                if mon_screen is not None:
                    tmpl = self._get_template("img/fixnetv3.png")
                    if tmpl is not None:
                        res = cv2.matchTemplate(mon_screen, tmpl, cv2.TM_CCOEFF_NORMED)
                        _, max_val, _, _ = cv2.minMaxLoc(res)
                        
                        if max_val >= 0.8:
                            self._fixnetv3_count += 1
                            print(f"[{self.device_id}] [MONITOR] fixnetv3.png detected (#{self._fixnetv3_count})! Tapping (472, 361)...")
                            self.tap(472, 361)
                            
                            if self._fixnetv3_count >= 8:
                                print(f"[{self.device_id}] [MONITOR] fixnetv3.png persists after 8 clicks! Force-stopping app...")
                                self._need_restart = True
                                self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
                                self._fixnetv3_count = 0
                        else:
                            if self._fixnetv3_count > 0:
                                self._fixnetv3_count = 0
                
            except Exception:
                pass
            time.sleep(3)

    def swipe(self, x1, y1, x2, y2, duration=300):
        self.last_activity_time = time.time()
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "input", "swipe", 
                     str(x1), str(y1), str(x2), str(y2), str(duration)])

    def check_black_screen(self):
        """Check if screen is mostly black (>80% black pixels) using persistence (15s)"""
        if self._screen is None:
            return False 
            
        try:
            # Thresholding to find black pixels (brightness < 50)
            _, thresh = cv2.threshold(self._screen, 50, 255, cv2.THRESH_BINARY_INV)
            num_black = cv2.countNonZero(thresh)
            total = self._screen.shape[0] * self._screen.shape[1]
            black_ratio = num_black / total
            is_black_now = black_ratio > 0.85
        except:
            is_black_now = False

        if not is_black_now:
            self._black_start_time = None
            return False
            
        if not hasattr(self, '_black_start_time') or self._black_start_time is None:
            self._black_start_time = time.time()
            return False
            
        duration = time.time() - self._black_start_time
        if duration >= 10:
            print(f"[{self.device_id}] STUCK/BLACK screen persisted for {duration:.1f}s. Triggering recovery...")
            self._black_start_time = None 
            return True
            
        return False

    def check_floating_popups(self):
        """
        Check and click floating popups (checkline / fixnetv2 / fixplay / fixnet1).
        เจอก็กด วนเช็คซ้ำจนกว่าจะไม่เจอ popup ใดๆ
        ทำงานทุกรอบ capture_screen() คลุมทั้งไฟล์
        """
        # checkline.png: Handle Checkbox Popup Sequence
        if self.exists_in_cache("img/checkline.png"):
            print(f"[{self.device_id}] [POPUP] checkline.png detected! Running special sequence...")
            self.click("img/checkline.png")
            sleep(2)
            
            # 1. Wait for @check-l1.png
            start_l1 = time.time()
            while time.time() - start_l1 < 60:
                self._raw_capture()
                if self.exists_in_cache("img/check-l1.png"):
                    print(f"[{self.device_id}] [POPUP] Found check-l1.png")
                    break
                sleep(1)
            
            # 2. Coordinates
            print(f"[{self.device_id}] [POPUP] Clicking coordinates (932, 133), (930, 253), (926, 327)...")
            self.tap(932, 133)
            sleep(5)
            self.tap(930, 253)
            sleep(5)
            self.tap(926, 327)
            sleep(5)
            
            # 3. Wait for check-l4.png
            start_l4 = time.time()
            while time.time() - start_l4 < 60:
                self._raw_capture()
                if self.exists_in_cache("img/check-l4.png"):
                    print(f"[{self.device_id}] [POPUP] Found and clicking check-l4.png")
                    self.click("img/check-l4.png")
                    break
                sleep(1)
                
            # 4. Click check-ok1.png
            print(f"[{self.device_id}] [POPUP] Waiting for check-ok1.png to finish...")
            for _ in range(60):
                self._raw_capture()
                if self.exists_in_cache("img/check-ok1.png"):
                    self.click("img/check-ok1.png")
                    print(f"[{self.device_id}] [POPUP] Checkline sequence complete!")
                    sleep(1)
                    self._raw_capture() # Update cache for caller
                    break
                sleep(1)
            return

        # fixnetv2.png: เจอก็กด แล้วรอกด fixnetv2ok.png
        if self.exists_in_cache("img/fixnetv2.png"):
            print(f"[{self.device_id}] [POPUP] fixnetv2.png detected, clicking...")
            self.click("img/fixnetv2.png")
            sleep(2)
            self._raw_capture()
            if self.exists_in_cache("img/fixnetv2ok.png"):
                self.click("img/fixnetv2ok.png")
                sleep(1)
                self._raw_capture() # Update cache for caller
            return

        if self.exists_in_cache("img/fixplay.png"):
            print(f"[{self.device_id}] [POPUP] fixplay.png detected, clicking...")
            self.click("img/fixplay.png")
            sleep(2)
            # After fixplay, FORCE wait and click check-ok1.png
            print(f"[{self.device_id}] [POPUP] Waiting for check-ok1.png after fixplay...")
            for _ in range(120):  # Wait up to 120 seconds
                self._raw_capture()
                if self.exists_in_cache("img/check-ok1.png"):
                    print(f"[{self.device_id}] [POPUP] check-ok1.png found after fixplay, clicking...")
                    self.click("img/check-ok1.png")
                    sleep(1)
                    self._raw_capture() # Update cache for caller
                    break
                sleep(1)

        # fixnet.png: เช็คตลอดเจอก็กดรัวๆ ไม่มีหยุดจนกว่าจะหายไป
        fixnet_clicks = 0
        while self.exists_in_cache("img/fixnet.png", similarity=0.8):
            fixnet_clicks += 1
            print(f"[{self.device_id}] [POPUP] fixnet.png detected (click #{fixnet_clicks}), clicking...")
            self.click("img/fixnet.png", similarity=0.8)
            sleep(1.5)
            self._raw_capture()
            if fixnet_clicks >= 10:
                print(f"[{self.device_id}] [POPUP] fixnet.png clicked 10 times, breaking to avoid infinite loop")
                break

        # fixnet1.png: วนเช็คซ้ำจนกว่าจะไม่เจอ (re-capture ทุกรอบ) - ปรับ similarity เป็น 0.8 เพื่อความชัวร์
        fixnet1_clicks = 0
        while self.exists_in_cache("img/fixnet1.png", similarity=0.8):
            fixnet1_clicks += 1
            print(f"[{self.device_id}] [POPUP] fixnet1.png detected (click #{fixnet1_clicks}), clicking...")
            self.click("img/fixnet1.png", similarity=0.8)
            sleep(1.5)
            self._raw_capture()  # จับภาพใหม่เพื่อเช็คซ้ำ (ไม่วนกลับ popup check)
            if fixnet1_clicks >= 10:
                print(f"[{self.device_id}] [POPUP] fixnet1.png clicked 10 times, breaking to avoid infinite loop")
                break

        # fixnetv3.png: Network error popup - tap (472, 361) to dismiss
        if self.exists_in_cache("img/fixnetv3.png", similarity=0.8):
            self._fixnetv3_count += 1
            print(f"[{self.device_id}] [POPUP] fixnetv3.png detected (#{self._fixnetv3_count}), tapping (472, 361)...")
            self.tap(472, 361)
            sleep(1.5)
            self._raw_capture()
            
            if self._fixnetv3_count >= 8:
                print(f"[{self.device_id}] [POPUP] fixnetv3.png persists after 8 clicks! Force-stopping app...")
                self._need_restart = True
                self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
                self._fixnetv3_count = 0

        if self.exists_in_cache("img/fixaccep.png"):
            print(f"[{self.device_id}] [POPUP] fixaccep.png detected, clicking...")
            self.click("img/fixaccep.png")
            sleep(1)

        # fixokk.png Persistence Check (Increased to 15s)
        if self.exists_in_cache("img/fixokk.png", similarity=0.8):
            if self._fixokk_start is None:
                self._fixokk_start = time.time()
                print(f"[{self.device_id}] [POPUP] Detected fixokk.png! Trying cancel.png immediately...")
                self.click("img/cancel.png", similarity=0.8)
                sleep(1.0) # Quick wait after cancel attempt
            elif time.time() - self._fixokk_start >= 15:
                print(f"[{self.device_id}] [POPUP] fixokk.png persists 15s! Clicking fixokk.png now...")
                self.click("img/fixokk.png", similarity=0.8)
                self._fixokk_start = None
                sleep(2)
        else:
            self._fixokk_start = None

        # alert2.png Persistence Check (8s)
        if self.exists_in_cache("img/alert2.png", similarity=0.8):
            if self._alert2_start is None:
                self._alert2_start = time.time()
                print(f"[{self.device_id}] [POPUP] Detected alert2.png, waiting 8s to force restart...")
            elif time.time() - self._alert2_start >= 8:
                print(f"[{self.device_id}] [RESTART] alert2.png persists 8s! Forcing restart...")
                self._need_restart = True
                self._alert2_start = None
                sleep(2)
        else:
            self._alert2_start = None

    def _raw_capture(self):
        """Capture screen WITHOUT triggering popup checks (ป้องกันวนซ้อน)"""
        try:
            kwargs = {}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                [self.adb_cmd, "-s", self.device_id, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10, **kwargs
            )
            if result.returncode == 0 and len(result.stdout) > 100:
                img_array = np.frombuffer(result.stdout, np.uint8)
                self._screen = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
                self._screen_raw_png = result.stdout
                self._screen_color = None  # lazy decode
            else:
                with open(self.filename, "wb") as f:
                    f.write(result.stdout)
                self._screen = cv2.imread(self.filename, 0)
                self._screen_raw_png = None
                self._screen_color = cv2.imread(self.filename, cv2.IMREAD_COLOR)
        except Exception as e:
            print(f"[{self.device_id}] Raw capture error: {e}")

    def check_error_images(self, skip_fixcak=False, skip_icon=False):
        """Check error images using cached screen"""

        # ===== FLOATING POPUP CHECKS (กดแล้วทำงานต่อ ไม่ return error) =====
        self.check_floating_popups()
        
        # Check for Black/Stuck screen
        # [REMOVED] User requested to only check black screen upon startup.

        # fixcak.png: restart process if found
        if not skip_fixcak:
            fixcak_path = "img/fixcak.png"
            if os.path.exists(fixcak_path) and self.exists_in_cache(fixcak_path):
                return "fixcak"
        
        # stopcheck.png: complete/stop process if found
        # Try multiple thresholds like in example code
        for th in [0.95, 0.9, 0.85, 0.8]:
            if self.exists_in_cache("img/stopcheck.png", similarity=th):
                return "stopcheck"
        
        # Common login errors
        if self.exists_in_cache("img/fixbuglogin.png"):
            return "fixbug"
            
        if self.exists_in_cache("img/unkhow.png"):
            return "unkhow"
            
        # App crash check: เช็คว่าแอปยังรันอยู่ไหม (ใช้ pidof แทน icon.png)
        if not skip_icon:
            try:
                pid_result = subprocess.run(
                    [self.adb_cmd, "-s", self.device_id, "shell", "pidof", "com.linecorp.LGRGS"],
                    capture_output=True, text=True, timeout=5
                )
                pid = pid_result.stdout.strip()
                if not pid:
                    return "icon"  # App not running → relaunch
            except:
                pass  # ถ้าเช็คไม่ได้ก็ข้ามไป
            
        if self.exists_in_cache("img/kaiby.png"):
            return "kaiby"

        if self.exists_in_cache("img/kaiby1.png"):
            return "kaiby"

        error_images = ["img/failed1.png", "img/fixalerterror1.png"]
        for err in error_images:
            if self.exists_in_cache(err):
                return "error_img"
                
        return None

    # =========================================================
    # OCR Methods - For Gear Mode
    # =========================================================
    def ocr_read_region(self, x, y, w, h):
        """Read text from a specific region of the cached color screen using EasyOCR."""
        if self._screen_color is None or not self.do_gear:
            return []
        
        # Crop region from color image
        img = self._screen_color[y:y+h, x:x+w]
        
        if img is None or img.size == 0:
            print(f"[{self.device_id}] OCR crop region empty!")
            return []
        
        reader = get_ocr_reader()
        results = reader.readtext(img, detail=1)
        
        text_results = []
        for (bbox, text, conf) in results:
            if conf > 0.3:
                text_results.append((text, conf))
        
        return text_results

    def ocr_read_full_screen(self):
        """Read all text from the full cached color screen."""
        if self._screen_color is None or not self.do_gear:
            return []
        
        region = self.ocr_region
        return self.ocr_read_region(region["x"], region["y"], region["w"], region["h"])

    def check_gears_on_screen(self):
        """Check for specific gear names using OCR on target region"""
        if not self.do_gear:
            return set()
            
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
                ocr_text = gear_data.get("ocr", gear_key)
                gear_name = gear_data.get("name", gear_key)
            else:
                ocr_text = gear_data
                gear_name = gear_data
            
            if ocr_text.lower() in all_text:
                found_gears.add(gear_name)
                print(f"[{self.device_id}] Found gear: {gear_name}")
                
        return found_gears

    def handle_post_login_tasks(self):
        """Perform additional tasks after reaching the lobby (Boxes, 7-Day, etc.)"""
        print(f"[{self.device_id}] Starting Post-Login Tasks...")
        try:
            load_config() # Reload global config (consolidated)
            self.cfg = config 
        except:
            pass
        
        # Helper to check first image with timeout
        def check_task_available(img_name, timeout=8):
            start = time.time()
            while time.time() - start < timeout:
                self.capture_screen()
                if self.exists_in_cache(img_name): return True
                sleep(1)
            return False

        # --- เพิ่มการรอหน้า Lobby ให้ชัวร์ก่อนเริ่ม (รอสูงสุด 20 วินาที) ---
        print(f"[{self.device_id}] Waiting for Lobby icons to load (up to 20s)...")
        lobby_ready = False
        lobby_start = time.time()
        while time.time() - lobby_start < 20:
            self.capture_screen()
            # เช็คว่าเจอไอคอนหลักๆ ในหน้า Lobby หรือยัง (เช่น กล่อง หรือ กาชา หรือ 7วัน)
            if self.exists_in_cache("img/box1.png") or self.exists_in_cache("img/gacha.png") or self.exists_in_cache("img/7day.png"):
                print(f"[{self.device_id}] Lobby ready! Icons detected.")
                lobby_ready = True
                break
            sleep(1.5)
        
        if not lobby_ready:
            print(f"[{self.device_id}] [WARN] Lobby icons not found after 20s wait. Proceeding anyway...")
        # -----------------------------------------------------------

        # 1. Check 7-Day Login
        if self.cfg.get("7day"):
            print(f"[{self.device_id}] Task Check: 7-Day Login...")
            if check_task_available("img/7day.png"):
                self.process_sequence(self.seven_day_seq)
                sleep(2)
            else:
                print(f"[{self.device_id}] 7-Day icon not found, skipping.")

        # 2. Open Gift Boxes (Round 1)
        box_cfg = self.cfg.get("box_settings", {})
        if box_cfg.get("first_round"):
            print(f"[{self.device_id}] Task Check: Opening Boxes (Round 1)...")
            # Usually box icon is always there or we can just try once
            if check_task_available("img/box1.png"):
                self.process_sequence(self.box_seq)
                sleep(2)
            else:
                print(f"[{self.device_id}] Box icon not found, skipping.")

        # 3. LEONARD Gacha Shop
        if self.cfg.get("shopgacha"):
            print(f"[{self.device_id}] Task Check: Leonard Gacha Shop...")
            if check_task_available("img/gacha.png"):
                self.process_shopgacha()
                sleep(2)
            else:
                print(f"[{self.device_id}] Gacha icon not found, skipping.")

        # 4. Swap Shop (Auto Trade)
        if self.cfg.get("swap_shop") or self.cfg.get("swap_shopevent") or self.cfg.get("auto_trade", {}).get("enabled"):
            print(f"[{self.device_id}] Task Check: Auto Trade / Swap Shop...")
            # We check for gacha.png as entry point for the new process_swap_shop
            if check_task_available("img/gacha.png"):
                res = self.process_swap_shop()
                if res in ["restart", "fixid", "fixunkown", "apple"]: return "restart"
                if res == "random-Fail": return "random-Fail"
                if res == "backup_complete": return "backup_complete"
                if res == "kaiby": return "kaiby"
                if res == "swap_shopevent":
                    self.process_swap_shopevent()
                
                # Check for individual auto_trade counts from config
                auto_trade_cfg = self.cfg.get("auto_trade", {})
                if auto_trade_cfg.get("enabled"):
                    self.auto_trade()
                
                sleep(2)
            else:
                print(f"[{self.device_id}] Gacha icon (for Swap Shop) not found, skipping.")
            
        # 5. Open Gift Boxes (Round 2)
        if box_cfg.get("second_round"):
            print(f"[{self.device_id}] Task Check: Opening Boxes (Round 2)...")
            if check_task_available("img/box1.png"):
                self.process_sequence(self.box_seq)
                sleep(2)
            else:
                print(f"[{self.device_id}] Box icon (Round 2) not found, skipping.")
            
        # 6. Gear / Ruby Gacha / Check Gear (Placeholders)
        if self.cfg.get("ruby-gear200") or self.cfg.get("random-gear") or self.cfg.get("check-gear"):
            print(f"[{self.device_id}] Task Check: Gear Functions (In development)...")

        # 7. Channel Switching (Placeholder)
        if self.cfg.get("channels_img"):
            print(f"[{self.device_id}] Task Check: Channel Switch to {self.cfg.get('channel', 'ch2')}...")

        print(f"[{self.device_id}] Post-Login Tasks Completed.")

    # =========================================================
    # FIND RANGER PROCESS (Unified from ranger-gear.py)
    # =========================================================
    def process_find_ranger(self, current_file):
        """Process find-ranger sequence - Returns results dict"""
        # Check both global and UI config
        is_enabled = self.do_ranger or config.get("find_ranger", 0)
        if not is_enabled:
            return {}
        
        print(f"\n[{self.device_id}] === Starting FIND-RANGER Process ===\n")
        results = {}
        
        # Step 1 & 2: Navigation to search screen
        print(f"[{self.device_id}] Starting persistent navigation (Searching for sec1/sec2)...")
        sec1_clicked = False
        while True:
            self.capture_screen()
            self.check_floating_popups()
            
            # Check for crash while waiting
            try:
                pid_result = subprocess.run(
                    [self.adb_cmd, "-s", self.device_id, "shell", "pidof", "com.linecorp.LGRGS"],
                    capture_output=True, text=True, timeout=5
                )
                if not pid_result.stdout.strip():
                    print(f"[{self.device_id}] App crashed, relaunching...")
                    self.open_app()
                    sleep(5)
                    sec1_clicked = False
            except: pass

            if self.exists_in_cache("img/sec2.png"):
                print(f"[{self.device_id}] Reached search screen (sec2), clicking to confirm...")
                self.click("img/sec2.png")
                break
                
            if not sec1_clicked and self.exists_in_cache("img/sec1.png"):
                print(f"[{self.device_id}] Found sec1, clicking once then waiting for sec2...")
                self.click("img/sec1.png")
                sec1_clicked = True
                sleep(3)
            sleep(1.5)
            
        print(f"[{self.device_id}] Reached search screen successfully.")
        
        for i, character in enumerate(self.characters):
            print(f"\n[{self.device_id}] --- Character {i+1}/{len(self.characters)}: {character} ---")
            self.tap(388, 288) # Search box
            sleep(0.3)
            self.type_text(character)
            sleep(0.5)
            
            if not self.wait_and_click_image("sec3.png", timeout=15, similarity=0.95): continue
            if not self.wait_and_click_image("sec4.png", timeout=15, similarity=0.95): continue
            
            sleep(2.0) # Wait for results
            
            current_found = False
            matching_files = self.ranger_files
            for attempt in range(2):
                if attempt > 0: sleep(1.0)
                self.capture_screen()
                self.check_floating_popups()
                for ranger_img in matching_files:
                    if self.exists_in_cache(f"img/{ranger_img}", similarity=0.95):
                        file_base = ranger_img.split('/')[-1].replace(".png", "")
                        found_hero_name = file_base
                        if isinstance(self.ranger_image_mapping, dict) and ranger_img in self.ranger_image_mapping:
                            data = self.ranger_image_mapping[ranger_img]
                            if isinstance(data, dict):
                                hero_name = data.get("hero", found_hero_name)
                                folder_name = data.get("folder", hero_name)
                            else:
                                hero_name = found_hero_name
                                folder_name = str(data)
                        else:
                            hero_name = found_hero_name
                            folder_name = hero_name
                        
                        results[hero_name] = folder_name
                        current_found = True
                        print(f"[{self.device_id}] Found ranger: {ranger_img} -> hero: {hero_name}, folder: {folder_name}")
                if current_found: break
            
            if not self.wait_and_click_image("sec5.png", timeout=15): pass
            if i < len(self.characters) - 1:
                if not self.wait_and_click_image("sec2.png", timeout=15): break
        
        print(f"[{self.device_id}] Find-Ranger complete.")
        return results

    def backup_ranger_results(self, results, gear_results=None):
        """Save backup based on results"""
        filename = self.current_original_filename or "unknown.xml"
        source_path = "/data/data/com.linecorp.LGRGS/shared_prefs/_LINE_COCOS_PREF_KEY.xml"
        safe_dev = self.device_id.replace(":", "_")
        temp_remote = f"/data/local/tmp/backup_{safe_dev}.xml"
        
        try:
            self.adb_shell(f"su -c 'cp {source_path} {temp_remote}'")
            self.adb_shell(f"su -c 'chmod 666 {temp_remote}'")
            
            # Combine all names for folder
            all_names = []
            if results: all_names.extend(results.values())
            if gear_results: all_names.extend(list(gear_results))
            
            if all_names:
                folder_name = "+".join(sorted(set(all_names)))
                backup_dir = os.path.join("backup-id", folder_name)
                if not os.path.exists(backup_dir): os.makedirs(backup_dir)
                dst = os.path.join(backup_dir, filename)
                self.adb_run([self.adb_cmd, '-s', self.device_id, 'pull', temp_remote, dst])
                print(f"[{self.device_id}] Backed up to: {dst}")
            else:
                not_found_dir = "not-found"
                if not os.path.exists(not_found_dir): os.makedirs(not_found_dir)
                dst = os.path.join(not_found_dir, filename)
                self.adb_run([self.adb_cmd, '-s', self.device_id, 'pull', temp_remote, dst])
                print(f"[{self.device_id}] Backed up to not-found: {dst}")
            
            self.adb_shell(f"rm -f {temp_remote}")
        except Exception as e:
            print(f"[{self.device_id}] Backup error: {e}")

    # =========================================================
    # CHECK GEAR PROCESS (Unified from ranger-gear.py)
    # =========================================================
    def process_check_gear(self, current_file, ranger_results=None, skip_findgear1=False):
        """Process check-gear sequence"""
        # Check all possible gear toggles from both configs
        is_enabled = (self.do_gear or 
                      config.get("check-gear", 0) or 
                      config.get("ruby-gear200", 0) or 
                      config.get("random-gear", 0))
        
        if not is_enabled:
            return set()
        
        print(f"\n[{self.device_id}] === Starting CHECK-GEAR Process ===\n")
        if not skip_findgear1:
            if not self.wait_and_click_image("findgear1.png"): return set()
        
        if not self.wait_and_click_image("findgear2.png"): return set()
        if not self.wait_and_click_image("findgear3.png"): return set()
        
        all_found_gears = set()
        # Attempt 1
        if self.wait_and_click_image("checkgear2.png") and self.wait_and_click_image("checkgear3.png", timeout=15):
            all_found_gears.update(self.check_gears_on_screen())
            sleep(1)
            # Tabs
            for tab in ["weapons1.png", "weapons2.png"]:
                self.capture_screen()
                if self.exists_in_cache(f"img/{tab}"):
                    self.click(f"img/{tab}")
                    sleep(2)
                    all_found_gears.update(self.check_gears_on_screen())
        else:
            # Fallback
            for tab in ["weapons1.png", "weapons2.png"]:
                self.capture_screen()
                if self.exists_in_cache(f"img/{tab}"):
                    self.click(f"img/{tab}")
                    sleep(2)
                    all_found_gears.update(self.check_gears_on_screen())
        
        return all_found_gears


    # =========================================================
    # ADB & Interaction
    # =========================================================
    def clear_specific_shared_prefs(self):
        """Delete ALL shared_prefs and clear app cache"""
        base = "/data/data/com.linecorp.LGRGS/shared_prefs"
        cache_dir = "/data/data/com.linecorp.LGRGS/cache"
        
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(1)
        
        # Total clear including cache (Restore to Full Clear)
        self.adb_shell(f"su -c 'rm -rf {base}/* && rm -rf {cache_dir}/*'")
        print(f"[{self.device_id}] Cleared shared_prefs + cache (Full)")

    def inject_file(self, local_xml_path):
        print(f"[{self.device_id}] Injecting file (Robust Mode)...")
        
        # ปลดล็อก Read-only (ถ้ามี)
        self.adb_shell("su -c 'mount -o remount,rw / 2>/dev/null || mount -o remount,rw /data 2>/dev/null'")
        
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)
        
        self.adb_shell("su -c 'killall -9 com.linecorp.LGRGS 2>/dev/null || true'")
        sleep(1)

        src = os.path.abspath(local_xml_path)
        tmp = f"/data/local/tmp/temp_pref_{self.device_id.replace(':','_')}.xml"
        final_dir = "/data/data/com.linecorp.LGRGS/shared_prefs"
        final = f"{final_dir}/_LINE_COCOS_PREF_KEY.xml"
        
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # Delete existing file first to ensure fresh injection as requested
                self.adb_shell(f"su -c 'rm -f {final}'")
                
                # Push to tmp
                result = self.adb_run([self.adb_cmd, "-s", self.device_id, "push", src, tmp], timeout=30)
                if result.returncode != 0:
                    err = result.stderr.decode('utf-8', errors='ignore') if result.stderr else 'Unknown Error'
                    print(f"[{self.device_id}] Push attempt {attempt} failed: {err}")
                    sleep(2)
                    continue
                
                # Copy, set permissions and owner + SYNC for 100% certainty
                shell_cmd = (
                    f"su -c '"
                    f"rm -f {final}; " # Use ; instead of && to ensure cp runs even if rm has weird error
                    f"cp {tmp} {final} && "
                    f"chmod 666 {final} && "
                    f"chown $(stat -c %u:%g {final_dir} 2>/dev/null || stat -c %u:%g {final_dir}/.. 2>/dev/null || echo 1000:1000) {final} || true && "
                    f"rm -f {tmp} && "
                    f"sync"
                    f"'"
                )
                self.adb_shell(shell_cmd)
                
                print(f"[{self.device_id}] Injection successful on attempt {attempt}")
                return local_xml_path
                    
            except Exception as e:
                print(f"[{self.device_id}] Attempt {attempt} error: {e}")
                sleep(2)
        
        print(f"[{self.device_id}] Injection FAILED after {max_retries} attempts!")
        return None

    def first_loop_process(self):
        try:
            print(f"[{self.device_id}] Starting First Loop Process (Turbo Mode)...")
            self.clear_specific_shared_prefs()
            sleep(1.5)
            
            # 1. Ensure we are at Home screen
            self.adb_shell("input keyevent 3")
            sleep(0.5)

            # 2. Sequence 1
            print(f"[{self.device_id}] Processing SEQ 1...")
            res1 = self.process_sequence(self.seq1)
            if res1 == "restart": return "restart"
            if res1 == "complete": return "complete"
            if res1 == "failed": return "failed"
            
            # 3. Back logic - Speed Mode (Triple Back)
            print(f"[{self.device_id}] Back Speed Mode: Executing Triple Back...")
            sleep(1) # Reduced from 4s
            for _ in range(3):
                self.adb_shell("input keyevent 4")
                sleep(0.2)
            sleep(0.5)
            
            # 4. Sequence 2
            print(f"[{self.device_id}] Processing SEQ 2...")
            res2 = self.process_sequence(self.seq2)
            if res2 == "restart": return "restart"
            if res2 == "complete": return "complete"
            if res2 == "failed": return "failed"
            
            # 5. End and Close App
            print(f"[{self.device_id}] First Loop Finished. Clearing app...")
            self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
            sleep(0.5)
            return "complete"
            
        except Exception as e:
            print(f"[{self.device_id}] First Loop Error: {e}")
            return "error"

    def process_sequence(self, sequence):
        idx = 0
        for item in sequence:
            idx += 1
            # Check for global triggers before each item
            self.capture_screen()
            # Skip icon check if we are currently looking for icon.png in sequence 
            # OR if we are at the very beginning of the sequence (app still launching)
            skip_icon = (item == 'icon.png' or idx <= 3)
            err = self.check_error_images(skip_icon=skip_icon)
            if err == "fixcak": return "restart"
            if err == "icon":
                print(f"[{self.device_id}] App closed/crashed! Relaunching with am start...")
                self.open_app()
                return "restart"
            if err == "stopcheck": return "complete"

            if isinstance(item, tuple):
                print(f"[{self.device_id}] Tapping: {item}")
                self.tap(item[0], item[1])
                sleep(3.5) # Increased to 3.5s for coordinate taps (checkboxes)
                continue
            
            if isinstance(item, str) and item.startswith('@'):
                checkpoint_img = item[1:]
                if not checkpoint_img.startswith('img'):
                    checkpoint_img = f"img/{checkpoint_img}"
                print(f"[{self.device_id}] Checkpoint: waiting for {checkpoint_img} (no click)")
                start_wait = time.time()
                while True:
                    if time.time() - start_wait > 480: # 8 minutes timeout
                        print(f"[{self.device_id}] TIMEOUT waiting for checkpoint {checkpoint_img}. Restarting first_loop...")
                        return "restart"

                    self.capture_screen()
                    
                    # ---- Check floating popups on every iteration ----
                    self.check_floating_popups()
                    # --------------------------------------------------
                    
                    err = self.check_error_images(skip_icon=skip_icon)
                    if err == "fixcak": return "restart"
                    if err == "fixbug":
                        self.click("img/fixbuglogin.png")
                        return "restart"
                    if err == "unkhow":
                        self.click("img/unkhow.png")
                        return "restart"
                    if err == "icon":
                        print(f"[{self.device_id}] App closed/crashed! Relaunching with am start...")
                        self.open_app()
                        return "restart"
                    if err == "stopcheck": return "complete"
                    
                    if self.exists_in_cache(checkpoint_img, similarity=0.95): 
                        print(f"[{self.device_id}] Checkpoint reached: {checkpoint_img}")
                        break
                    sleep(1.5)
                sleep(1.0)
                continue
                
            img_path = f"img/{item}" if isinstance(item, str) and not item.startswith('img') else item
            
            if item == 'icon.png':
                print(f"[{self.device_id}] Opening app via am start (instead of icon click)...")
                self.open_app()
                print(f"[{self.device_id}] App launched, waiting 4s...")
                sleep(4)
                continue

            # === SPECIAL CASE: apple.png ===
            # เจอ apple.png ให้กดด้วย และทำลูป fixid ต่อ
            # เจอ fixid ก่อน -> กด fixok -> refresh -> check -> วนเช็ค fixid ไปเรื่อยๆ
            # ถ้าเจอ fixid ครบ 8 รอบ -> return "failed" ส่งไป login-failed
            # ถ้าไม่เจอ fixid -> ผ่านไปต่อ step ถัดไป
            if item == 'apple.png':
                print(f"[{self.device_id}] Apple step: clicking apple.png (if found) and checking for fixid loop...")
                fixid_count = 0
                max_fixid_retries = 8
                apple_start_wait = time.time()
                
                while True:
                    self.capture_screen()
                    
                    # ---- Check floating popups on every iteration ----
                    self.check_floating_popups()
                    # --------------------------------------------------
                    
                    # Check errors first
                    err = self.check_error_images()
                    if err == "fixcak": return "restart"
                    if err == "fixbug":
                        self.click("img/fixbuglogin.png")
                        return "restart"
                    if err == "unkhow":
                        self.click("img/unkhow.png")
                        return "restart"
                    if err == "icon":
                        print(f"[{self.device_id}] App closed/crashed! Relaunching with am start...")
                        self.open_app()
                        return "restart"
                    if err == "stopcheck": return "complete"
                    
                    # === คลิก apple.png ถ้าเจอ ===
                    if self.exists_in_cache("img/apple.png"):
                        print(f"[{self.device_id}] Found apple.png! Clicking...")
                        self.click("img/apple.png")
                        sleep(2)
                        # ไม่ break นะครับ เพราะต้องเช็ค fixid ต่อ
                    
                    # === fixid1.png → failed ทันที ===
                    if self.exists_in_cache("img/fixid1.png", similarity=0.95):
                        print(f"[{self.device_id}] Found fixid1.png! -> login-failed immediately")
                        return "failed"

                    # === เจอ fixid.png -> เริ่ม loop: fixok -> refresh -> check ===
                    if self.exists_in_cache("img/fixid.png", similarity=0.95):
                        fixid_count += 1
                        print(f"[{self.device_id}] Found fixid.png ({fixid_count}/{max_fixid_retries})")
                        
                        if fixid_count >= max_fixid_retries:
                            print(f"[{self.device_id}] fixid limit reached ({max_fixid_retries} times)! Sending to login-failed...")
                            return "failed"
                        
                        # 1) กด fikcheck
                        print(f"[{self.device_id}] Step 1: clicking fikcheck.png...")
                        for _ in range(10): # Timeout 10s
                            self.capture_screen()
                            if self.exists_in_cache("img/fikcheck.png", similarity=0.8):
                                self.click("img/fikcheck.png", similarity=0.8)
                                print(f"[{self.device_id}] Clicked fikcheck.png")
                                sleep(2)
                                break
                            sleep(1)
                        
                        # 2) กด refresh
                        print(f"[{self.device_id}] Step 2: clicking refresh.png...")
                        for _ in range(10): # Timeout 10s
                            self.capture_screen()
                            if self.exists_in_cache("img/refresh.png"):
                                self.click("img/refresh.png")
                                print(f"[{self.device_id}] Clicked refresh.png")
                                sleep(3)
                                break
                            sleep(1)
                        
                        # 3) รอ check.png แล้วกด (timeout 60 วิ)
                        print(f"[{self.device_id}] Step 3: waiting for check.png...")
                        check_wait_start = time.time()
                        while time.time() - check_wait_start < 60:
                            self.capture_screen()
                            
                            err2 = self.check_error_images()
                            if err2 == "fixcak": return "restart"
                            if err2 == "fixbug":
                                self.click("img/fixbuglogin.png")
                                return "restart"
                            if err2 == "icon":
                                self.click("img/icon.png")
                                return "restart"
                            if err2 == "stopcheck": return "complete"
                            
                            if self.exists_in_cache("img/check.png"):
                                print(f"[{self.device_id}] Found check.png! Clicking...")
                                self.click("img/check.png")
                                sleep(2)
                                # หลังกด check -> รอดู fixid ก่อน 2 วิ
                                found_fixid_after_check = False
                                for _ in range(2):
                                    self.capture_screen()
                                    if self.exists_in_cache("img/fixid.png"):
                                        print(f"[{self.device_id}] Found fixid.png right after check! Re-routing...")
                                        found_fixid_after_check = True
                                        break
                                    sleep(1)
                                
                                if found_fixid_after_check:
                                    break

                                if self.exists_in_cache("img/fikcheck.png", similarity=0.8):
                                    print(f"[{self.device_id}] Found fikcheck.png after check! Clicking...")
                                    self.click("img/fikcheck.png", similarity=0.8)
                                    sleep(1)
                                break
                            
                            sleep(1)
                        
                        # วนกลับไปเช็ค fixid อีกรอบ
                        continue
                    
                    # === ไม่เจอ fixid และถ้าคลิก apple ไปแล้ว หรือรอสักพักแล้วไม่เจอ fixid -> ผ่านไปได้เลย ===
                    # ตรวจสอบเพิ่มเติมว่าเราข้ามขั้นตอน apple ได้เมื่อไหร่
                    if time.time() - apple_start_wait > 30:
                        print(f"[{self.device_id}] Apple step finished (waited 30s or check passed).")
                        break
                    
                    sleep(1)

                    
                continue  # ไปต่อ item ถัดไปใน sequence

            print(f"[{self.device_id}] Waiting for {item}...")
            start_wait = time.time()
            
            # Custom timeout for specific images
            item_timeout = 480
            if item in ['box6.png', 'end_box.png']:
                item_timeout = 5

            while True:
                if time.time() - start_wait > item_timeout:
                    if item in ['box6.png', 'end_box.png']:
                        print(f"[{self.device_id}] Timeout 5s for {item}, skipping to next step.")
                        break # Continue to next item in sequence
                    print(f"[{self.device_id}] TIMEOUT waiting for {item}. Restarting first_loop...")
                    return "restart"

                # Check fixcak/stopcheck/blackscreen/fixbug/unkhow
                self.capture_screen() # Ensure screen is captured before checking errors
                
                # ---- Check floating popups on every iteration ----
                self.check_floating_popups()
                # --------------------------------------------------
                
                err = self.check_error_images()
                if err == "fixcak":
                    print(f"[{self.device_id}] Found fixcak.png! Restarting first loop...")
                    return "restart"
                if err == "fixbug":
                    print(f"[{self.device_id}] Found fixbuglogin.png! Clicking and restarting...")
                    self.click("img/fixbuglogin.png")
                    return "restart"
                if err == "unkhow":
                    print(f"[{self.device_id}] Found unkhow.png! Clicking and restarting...")
                    self.click("img/unkhow.png")
                    return "restart"
                if err == "icon":
                    print(f"[{self.device_id}] App closed/crashed! Clicking icon to relaunch...")
                    self.click("img/icon.png")
                    return "restart"
                if err == "stopcheck":
                    print(f"[{self.device_id}] Found stopcheck.png! Skipping to complete.")
                    return "complete"
                if err == "kaiby":
                    print(f"[{self.device_id}] ⚠️ พบ kaiby.png! (ไก่บี้เด้งต้อนรับ) ยกเลิกการ Login ทันที...")
                    self.clear_and_restart()
                    sleep(2)
                    return "kaiby"
                
                if self.exists_in_cache(img_path):
                    print(f"[{self.device_id}] Found {item}, clicking...")
                    self.click(img_path)
                    sleep(0.8) # Fast transition for images

                    # === SPECIAL CASE: box1.png logic ===
                    if item == 'box1.png':
                        print(f"[{self.device_id}] [BOX] box1 clicked. Waiting 20s for box2.png or end_box.png...")
                        found_cont = False
                        wait_box_started = time.time()
                        while time.time() - wait_box_started < 20:
                            self.capture_screen()
                            if self.exists_in_cache("img/box2.png"):
                                print(f"[{self.device_id}] [BOX] box2.png detected. Proceeding...")
                                found_cont = True
                                break
                            if self.exists_in_cache("img/end_box.png"):
                                print(f"[{self.device_id}] [BOX] end_box.png detected. Stopping box sequence.")
                                # We don't set found_cont=True because we want to jump to box5
                                break
                            sleep(1)
                        
                        if not found_cont:
                            print(f"[{self.device_id}] [BOX] Box2 not found or end reached. Clicking box5.png and finishing.")
                            # Try to click box5.png to close
                            for _ in range(10):
                                self.capture_screen()
                                if self.exists_in_cache("img/box5.png"):
                                    self.click("img/box5.png")
                                    print(f"[{self.device_id}] [BOX] Clicked box5.png")
                                    break
                                sleep(1)
                            return "success" # Exit this sequence early

                    break
                sleep(0.5) # Fast loop search
            
        return "success"

    def wait_and_click_image(self, img_name, timeout=30, similarity=0.95):
        """Wait for image and click it, return True if found (timeout in seconds)"""
        if not img_name.startswith('img'):
            img_path = f"img/{img_name}"
        else:
            img_path = img_name
        
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.capture_screen()
                # ---- Check floating popups on every iteration ----
                self.check_floating_popups()
                # --------------------------------------------------
                if self.exists_in_cache(img_path, similarity=similarity):
                    print(f"[{self.device_id}] Found {img_name} (sim={similarity})! Clicking...")
                    self.click(img_path, similarity=similarity)
                    return True
            except Exception as e:
                print(f"[{self.device_id}] Error while waiting for {img_name}: {e}")
            sleep(0.2)
                
        print(f"[{self.device_id}] Timeout waiting for {img_name} ({timeout}s)")
        return False

    # =========================================================
    # LOGIN SUCCESS BACKUP
    # =========================================================
    def backup_to_success(self, filename, source_path):
        # Disabled moving to login-success folder
        pass

    def clear_and_restart(self):
        """Clear app and prepare for next file"""
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)

    # =========================================================
    # Main Login
    # =========================================================
    def main_login(self, current_filename):
        print(f"[{self.device_id}] Starting Main Login...")
        self._login_fixid_count = 0  # Reset fixid counter for each new ID
        
        # Clear app
        self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
        sleep(2)
        
        # เปิดแอปด้วย am start (เร็วกว่าและเสถียรกว่าคลิก icon.png)
        self.open_app()
        sleep(3)
        
        # === Black Screen Check หลังเปิดแอพ (8 วิ ถ้ายังดำ/เทา > 75% → clear + restart) ===
        for black_attempt in range(3):  # ลองได้ 3 ครั้ง
            black_start = time.time()
            is_stuck = False
            while time.time() - black_start < 8:
                self.capture_screen()
                if self._screen is not None:
                    try:
                        _, thresh = cv2.threshold(self._screen, 50, 255, cv2.THRESH_BINARY_INV)
                        num_black = cv2.countNonZero(thresh)
                        total = self._screen.shape[0] * self._screen.shape[1]
                        black_ratio = num_black / total
                        if black_ratio < 0.85:
                            # จอสว่างแล้ว (>15% pixels not black)
                            print(f"[{self.device_id}] [BLACK] Screen OK! (app loaded)")
                            is_stuck = False
                            break
                        else:
                            is_stuck = True
                    except:
                        is_stuck = True
                else:
                    is_stuck = True
                sleep(1)
            
            if is_stuck:
                print(f"[{self.device_id}] [BLACK] Dark screen 8s after launch! (attempt {black_attempt+1}/3) Clearing...")
                self.clear_and_restart()
                self.open_app()
                sleep(3)
            else:
                break  # แอพโหลดสำเร็จ ออกจาก loop
            
        loop_count = 0
        status = "unknown"
        event_passed = False  # หลังเจอ event.png แล้วหยุดเช็ค fixok

        
        while True:
            # 0. Check if background monitor triggered a restart
            if self._need_restart:
                print(f"[{self.device_id}] Main loop detected restart request from monitor.")
                self._need_restart = False
                # On restart, we continue the loop which will naturally restart the login flow
                self.clear_and_restart()
                self.open_app()
                sleep(5)
                continue

            loop_count += 1
            if loop_count % 5 == 0:
                print(f"[{self.device_id}] Login loop iteration {loop_count}")

            self.capture_screen()

            # === เช็คว่าเกมยังรันอยู่จริงไหม (เช็คทุกๆ 15 รอบ ป้องกันหน่วง) ===
            if loop_count % 15 == 0:
                try:
                    pid_result = subprocess.run(
                        [self.adb_cmd, "-s", self.device_id, "shell", "pidof", "com.linecorp.LGRGS"],
                        capture_output=True, text=True, timeout=5
                    )
                    if not pid_result.stdout.strip():
                        print(f"[{self.device_id}] [CRASH] App not running! Relaunching...")
                        self.open_app()
                        sleep(5)
                        continue
                except:
                    pass

            # ===== FLOATING POPUP CHECKS (กดแล้วทำงานต่อ) =====
            self.check_floating_popups()

            # fixnetv3.png Check in login loop
            if self.exists_in_cache("img/fixnetv3.png", similarity=0.8):
                print(f"[{self.device_id}] [POPUP] fixnetv3.png detected in login loop! Tapping (472, 361)...")
                self.tap(472, 361)
                sleep(0.5)
                continue

            # === fixokk.png Persistence Check (รอค้างครบ 5 วิ ถึงจะกด) ===
            if self.exists_in_cache("img/fixokk.png", similarity=0.8):
                if not hasattr(self, '_fixokk_start_time') or self._fixokk_start_time is None:
                    self._fixokk_start_time = time.time()
                    print(f"[{self.device_id}] Detected fixokk.png... waiting 5s")
                elif time.time() - self._fixokk_start_time >= 5:
                    print(f"[{self.device_id}] ⚠️ fixokk.png ค้างอยู่ครบ 5 วินาที! ทำการกด...")
                    self.click("img/fixokk.png", similarity=0.8)
                    self._fixokk_start_time = None
                    sleep(2)
            else:
                self._fixokk_start_time = None

            # === alert2.png Persistence Check (รอค้างครบ 8 วิ ให้ clear app แล้วเปิดใหม่) ===
            if self.exists_in_cache("img/alert2.png", similarity=0.8):
                if not hasattr(self, '_alert2_start_time') or self._alert2_start_time is None:
                    self._alert2_start_time = time.time()
                    print(f"[{self.device_id}] Detected alert2.png... waiting 8s to clear app")
                elif time.time() - self._alert2_start_time >= 8:
                    print(f"[{self.device_id}] ⚠️ alert2.png ค้างอยู่ครบ 8 วินาที! เคลียร์แอพและเข้าใหม่...")
                    self.clear_and_restart()
                    self.open_app()
                    self._alert2_start_time = None
                    sleep(3)
                    continue
            else:
                self._alert2_start_time = None


            # === fixid1.png → failed ทันที ===
            if self.exists_in_cache("img/fixid1.png", similarity=0.95):
                print(f"[{self.device_id}] Found fixid1.png! -> login-failed immediately")
                self._login_fixid_count = 0
                return "failed"

            # === fixid.png Check (เช็คทุกรอบ) -> fixok -> refresh -> check ===
            if self.exists_in_cache("img/fixid.png", similarity=0.95):
                self._login_fixid_count += 1
                print(f"[{self.device_id}] Found fixid.png ({self._login_fixid_count}/8), fixok -> refresh -> check...")
                
                if self._login_fixid_count >= 8:
                    print(f"[{self.device_id}] fixid limit reached (8 times)! Failing...")
                    self._login_fixid_count = 0
                    return "failed"
                
                # 1) กด fikcheck
                print(f"[{self.device_id}] Step 1: waiting for fikcheck.png (10s timeout)...")
                sleep(1.5) # ให้หน้าจอเสถียรหลัง re-route
                for _ in range(10): # Timeout 10s
                    self.capture_screen()
                    if self.exists_in_cache("img/fikcheck.png", similarity=0.8):
                        self.click("img/fikcheck.png", similarity=0.8)
                        print(f"[{self.device_id}] Clicked fikcheck.png")
                        sleep(2)
                        break
                    sleep(1)
                
                # 2) กด refresh
                print(f"[{self.device_id}] Step 2: clicking refresh.png (10s timeout)...")
                for _ in range(10): # Timeout 10s
                    self.capture_screen()
                    if self.exists_in_cache("img/refresh.png"):
                        self.click("img/refresh.png")
                        print(f"[{self.device_id}] Clicked refresh.png")
                        sleep(3)
                        break
                    sleep(1)
                
                # 3) รอ check.png แล้วกด
                print(f"[{self.device_id}] Step 3: waiting for check.png (60s timeout)...")
                check_wait_start = time.time()
                while time.time() - check_wait_start < 60:
                    self.capture_screen()
                    if self.exists_in_cache("img/check.png"):
                        print(f"[{self.device_id}] Found check.png! Clicking...")
                        self.click("img/check.png")
                        sleep(2)
                        # หลังกด check -> รอดู fixid ก่อน 2 วิ
                        found_fixid_after_check = False
                        for _ in range(2):
                            self.capture_screen()
                            if self.exists_in_cache("img/fixid.png"):
                                print(f"[{self.device_id}] Found fixid.png right after check! Re-routing...")
                                found_fixid_after_check = True
                                break
                            sleep(1)
                        
                        if found_fixid_after_check:
                            break

                        if self.exists_in_cache("img/fikcheck.png", similarity=0.8):
                            print(f"[{self.device_id}] Found fikcheck.png after check! Clicking...")
                            self.click("img/fikcheck.png", similarity=0.8)
                            sleep(1)
                        break
                    sleep(1)
                
                continue

            # === เจอ refresh.png (ไม่มี fixid) -> กด refresh -> check ===
            if self.exists_in_cache("img/refresh.png"):
                print(f"[{self.device_id}] Found refresh.png (no fixid), clicking refresh -> check...")
                self.click("img/refresh.png")
                sleep(3)
                
                check_wait_start = time.time()
                while time.time() - check_wait_start < 60:
                    self.capture_screen()
                    if self.exists_in_cache("img/check.png"):
                        print(f"[{self.device_id}] Found check.png! Clicking...")
                        self.click("img/check.png")
                        sleep(2)
                        # หลังกด check -> รอดู fixid ก่อน 2 วิ
                        found_fixid_after_check = False
                        for _ in range(2):
                            self.capture_screen()
                            if self.exists_in_cache("img/fixid.png"):
                                print(f"[{self.device_id}] Found fixid.png right after check! Re-routing...")
                                found_fixid_after_check = True
                                break
                            sleep(1)
                        
                        if found_fixid_after_check:
                            break
                        
                        # หลังกด check -> หา fixok ด้วย
                        self.capture_screen()
                        if self.exists_in_cache("img/fixok.png"):
                            print(f"[{self.device_id}] Found fixok.png after check! Clicking...")
                            self.click("img/fixok.png")
                            sleep(1)
                        break
                    sleep(1)
                
                continue
            # ====================================================

            # Crash Check: ใช้ open_app แทนคลิก icon.png
            try:
                pid_result = subprocess.run(
                    [self.adb_cmd, "-s", self.device_id, "shell", "pidof", "com.linecorp.LGRGS"],
                    capture_output=True, text=True, timeout=5
                )
                if not pid_result.stdout.strip():
                    print(f"[{self.device_id}] App crashed during login. Relaunching...")
                    self.open_app()
                    sleep(5)
                    loop_count = 0
                    continue
            except:
                pass
            
            # fixalerterror1 Check
            if self.exists_in_cache("img/fixalerterror1.png"):
                print(f"[{self.device_id}] Alert error detected. Dimissing...")
                self.click("img/fixalerterror1.png")
                sleep(2)
                loop_count = 0
                continue

            # fixcak.png Check
            if self.exists_in_cache("img/fixcak.png"):
                print(f"[{self.device_id}] Fixcak detected (fix bug login). Dismissing...")
                self.click("img/fixcak.png")
                sleep(2)
                loop_count = 0
                continue
                
            # kaiby.png / kaiby1.png Check
            if self.exists_in_cache("img/kaiby.png", similarity=0.8) or self.exists_in_cache("img/kaiby1.png", similarity=0.8):
                print(f"[{self.device_id}] ⚠️ พบ kaiby.png! (ไก่บี้เด้งระหว่าง Login) เคลียร์แอพและส่งเข้าโฟลเดอร์ kaiby...")
                self.clear_and_restart()
                sleep(2)
                return "kaiby"
                
            # *** SUCCESS -> Just Login and Backup ***
            if self.exists_in_cache("img/stoplogin.png", similarity=0.8):
                # [DIST CHECK] แวะเช็คหา distcheck หรือ distskip 5วิ
                print(f"[{self.device_id}] stoplogin found, checking for distcheck/distskip (5s)...")
                found_distcheck = False
                found_distskip_early = False
                for _ in range(5):
                    if self.exists_in_cache("img/distcheck.png"):
                        found_distcheck = True
                        break
                    if self.exists_in_cache("img/distskip.png"):
                        found_distskip_early = True
                        break
                    sleep(1)
                    self.capture_screen()
                
                if found_distskip_early:
                    print(f"[{self.device_id}] [DIST] distskip.png found early! Handling sequence...")
                    self.click("img/distskip.png", similarity=0.8)
                    sleep(1)
                    
                    print(f"[{self.device_id}] [DIST] Waiting for stagespecal.png...")
                    while not self.exists("img/stagespecal.png", similarity=0.8):
                        sleep(1)
                    self.click("img/stagespecal.png", similarity=0.8)
                    sleep(1)

                    print(f"[{self.device_id}] [DIST] Waiting and clicking backdist.png until gone...")
                    # Wait for it to appear first
                    while not self.exists("img/backdist.png", similarity=0.8):
                        sleep(1)
                    # Click until it disappears
                    while True:
                        self.capture_screen()
                        if self.exists_in_cache("img/backdist.png", similarity=0.8):
                            self.click("img/backdist.png", similarity=0.8)
                            sleep(1.2)
                        else:
                            break

                    print(f"[{self.device_id}] [DIST] Waiting and clicking backdist1.png until gone...")
                    # Wait for it to appear first
                    while not self.exists("img/backdist1.png", similarity=0.8):
                        sleep(1)
                    # Click until it disappears
                    while True:
                        self.capture_screen()
                        if self.exists_in_cache("img/backdist1.png", similarity=0.8):
                            self.click("img/backdist1.png", similarity=0.8)
                            sleep(1.2)
                        else:
                            break
                    
                    print(f"[{self.device_id}] [DIST] dist sequence complete.")

                elif found_distcheck:
                    print(f"[{self.device_id}] [DIST] distcheck.png found! Entering full dist sequence...")
                    
                    # === Check for kaibyswap_shop.png before proceeding (if configured) ===
                    kaibyskip_enabled = config.get("kaibyskip", 0)
                    kaibycheck_enabled = config.get("kaibycheck", 0)
                    
                    if kaibycheck_enabled == 1:
                        print(f"[{self.device_id}] config kaibycheck=1: Skipping kaibyswap_shop check, proceeding normally...")
                    elif kaibyskip_enabled == 1:
                        print(f"[{self.device_id}] config kaibyskip=1: Checking for kaibyswap_shop.png (3s)...")
                        kaibyswap_found = False
                        kaiby_start = time.time()
                        while time.time() - kaiby_start < 3:
                            self.capture_screen()
                            if self.exists_in_cache("img/kaibyswap_shop.png"):
                                kaibyswap_found = True
                                break
                            sleep(0.5)
                        
                        if kaibyswap_found:
                            print(f"[{self.device_id}] ⚠️ Found kaibyswap_shop.png before DIST! Returning kaiby...")
                            self.clear_and_restart()
                            return "kaiby"
                    # ====================================================================

                    # 1. รอเจอ dist1.png และกด
                    print(f"[{self.device_id}] [DIST] Waiting for dist1.png...")
                    dist1_found = False
                    dist1_start = time.time()
                    while not dist1_found:
                        if time.time() - dist1_start > 120:
                            print(f"[{self.device_id}] [DIST] Timeout waiting for dist1.png (120s). Skipping.")
                            break
                        self.capture_screen()
                        if self.check_error_images() == "icon": self.open_app()
                        if self.exists_in_cache("img/dist1.png", similarity=0.8):
                            self.click("img/dist1.png", similarity=0.8)
                            print(f"[{self.device_id}] [DIST] Clicked dist1.png")
                            dist1_found = True
                        sleep(1)

                    # 2. รอเจอ waitdist.png
                    print(f"[{self.device_id}] [DIST] Waiting for waitdist.png...")
                    dist_pos = None
                    dist_wait_start = time.time()
                    while dist_pos is None:
                        if time.time() - dist_wait_start > 120:
                            print(f"[{self.device_id}] [DIST] Timeout waiting for waitdist.png (120s). Skipping.")
                            break
                        self.capture_screen()
                        if self.check_error_images() == "icon": self.open_app()
                        dist_pos = self._find_in_screen("img/waitdist.png", similarity=0.8)
                        if dist_pos:
                            print(f"[{self.device_id}] [DIST] Found waitdist at {dist_pos}")
                        sleep(1)

                    # 3. รอเจอ dist2.png และกด
                    if dist_pos:
                        print(f"[{self.device_id}] [DIST] Waiting for dist2.png...")
                        dist2_found = False
                        dist2_start = time.time()
                        while not dist2_found:
                            if time.time() - dist2_start > 120:
                                print(f"[{self.device_id}] [DIST] Timeout waiting for dist2.png (120s). Skipping.")
                                break
                            self.capture_screen()
                            if self.check_error_images() == "icon": self.open_app()
                            if self.exists_in_cache("img/dist2.png", similarity=0.8):
                                self.click("img/dist2.png", similarity=0.8)
                                print(f"[{self.device_id}] [DIST] Clicked dist2.png")
                                dist2_found = True
                            sleep(1)

                        # 4. กดตำแหน่ง waitdist 30 รอบ
                        if dist2_found:
                            print(f"[{self.device_id}] [DIST] Clicking saved position {dist_pos} 30 times...")
                            for _ in range(30):
                                self.tap(dist_pos[0], dist_pos[1])
                                sleep(0.05)

                            # 5. รอเจอ dist3.png และกด
                            print(f"[{self.device_id}] [DIST] Waiting for dist3.png...")
                            dist3_found = False
                            dist3_start = time.time()
                            while not dist3_found:
                                if time.time() - dist3_start > 120:
                                    print(f"[{self.device_id}] [DIST] Timeout waiting for dist3.png (120s). Skipping.")
                                    break
                                self.capture_screen()
                                if self.check_error_images() == "icon": self.open_app()
                                if self.exists_in_cache("img/dist3.png", similarity=0.8):
                                    self.click("img/dist3.png", similarity=0.8)
                                    print(f"[{self.device_id}] [DIST] Clicked dist3.png - Sequence Complete")
                                    dist3_found = True
                                sleep(1)

                    # 6. distskip -> stagespecal.png -> backdist -> กด ESC
                    print(f"[{self.device_id}] [DIST] Waiting for distskip.png...")
                    skip_start = time.time()
                    while not self.exists("img/distskip.png", similarity=0.8):
                        if time.time() - skip_start > 60: break
                        sleep(1)
                    self.click("img/distskip.png", similarity=0.8)
                    sleep(1)

                    print(f"[{self.device_id}] [DIST] Waiting for stagespecal.png...")
                    spec_start = time.time()
                    while not self.exists("img/stagespecal.png", similarity=0.8):
                        if time.time() - spec_start > 60: break
                        sleep(1)
                    self.click("img/stagespecal.png", similarity=0.8)
                    sleep(1)

                    print(f"[{self.device_id}] [DIST] Waiting and clicking backdist.png until gone...")
                    # Wait for it to appear first
                    backdist_wait_start = time.time()
                    while not self.exists("img/backdist.png", similarity=0.8):
                        if time.time() - backdist_wait_start > 60: break
                        sleep(1)
                    # Click until it disappears
                    while True:
                        self.capture_screen()
                        if self.exists_in_cache("img/backdist.png", similarity=0.8):
                            self.click("img/backdist.png", similarity=0.8)
                            sleep(1.2)
                        else:
                            break

                    print(f"[{self.device_id}] [DIST] Waiting and clicking backdist1.png until gone...")
                    # Wait for it to appear first
                    back1_wait_start = time.time()
                    while not self.exists("img/backdist1.png", similarity=0.8):
                        if time.time() - back1_wait_start > 60: break
                        sleep(1)
                    # Click until it disappears
                    while True:
                        self.capture_screen()
                        if self.exists_in_cache("img/backdist1.png", similarity=0.8):
                            self.click("img/backdist1.png", similarity=0.8)
                            sleep(1.2)
                        else:
                            break
                    
                    print(f"[{self.device_id}] [DIST] dist sequence complete.")


                print(f"[{self.device_id}] Login successful! (stoplogin detected)")
                
                # --- NEW: Perform tasks and scans according to config ---
                ranger_results = {}
                gear_results = set()

                # 1. Post-Login Tasks (Boxes, 7-Day, etc.)
                self.update_gui_status("Post-Login Tasks")
                post_status = self.handle_post_login_tasks()
                if post_status == "restart":
                    print(f"[{self.device_id}] Post-login task requested restart.")
                    continue
                if post_status == "random-Fail":
                    print(f"[{self.device_id}] Post-login task failed (random/gacha).")
                    return "random-Fail"
                if post_status == "backup_complete":
                     print(f"[{self.device_id}] Backup complete during post-login. Success.")
                     return "success"
                if post_status == "kaiby":
                     print(f"[{self.device_id}] Kaiby detected during post-login tasks.")
                     return "kaiby"
                
                # 2. Ranger Scan (Strict check)
                if self.do_ranger == 1 or self.cfg.get("find_ranger") == 1:
                    self.update_gui_status("Ranger Scan")
                    ranger_results = self.process_find_ranger(current_filename)
                
                # 3. Gear Scan (Strict check for any gear toggle)
                if (self.do_gear == 1 or 
                    self.cfg.get("check-gear") == 1 or 
                    self.cfg.get("ruby-gear200") == 1 or 
                    self.cfg.get("random-gear") == 1):
                    self.update_gui_status("Gear Scan")
                    gear_results = self.process_check_gear(current_filename, ranger_results)

                # 4. Backup Results
                self.update_gui_status("Backing up")
                self.backup_ranger_results(ranger_results, gear_results)

                # Update stats for display
                if ranger_results or gear_results:
                    all_found = list(ranger_results.keys()) + list(gear_results)
                    ui_stats.update_hero(f"Found: {len(all_found)} items")
                else:
                    ui_stats.update_hero("Success")
                
                msg = f"[{self.device_id}] 🏆 Success Login & Tasks!"
                if GUI_INSTANCE:
                    GUI_INSTANCE.log("SUCCESS", msg)
                else:
                    print(msg)

                # Clear app and restart for next ID (Wait 8s as requested by user)
                print(f"[{self.device_id}] Success! Waiting 8s before clearing...")
                self.update_gui_status("Cleaning up")
                sleep(8.0)
                self.clear_and_restart()
                return "success"
                
            # Kaiby / Kaiby1 Check (High Priority)
            if self.exists_in_cache("img/kaiby.png") or self.exists_in_cache("img/kaiby1.png"):
                reason = "kaiby1.png" if self.exists_in_cache("img/kaiby1.png") else "kaiby.png"
                print(f"[{self.device_id}] {reason} detected! Stopping login...")
                return "kaiby"

            # Failed
            if self.exists_in_cache("img/login-failed.png"):
                print(f"[{self.device_id}] Login failed (login-failed.png detected)")
                self._login_fixid_count = 0
                return "failed"
                
            # Error/Reset
            error_found = self.check_error_images()
            
            if error_found:
                print(f"[{self.device_id}] Error image found: {error_found}. Resetting...")
                if error_found in ["fixbug", "unkhow"]:
                    img = "img/fixbuglogin.png" if error_found == "fixbug" else "img/unkhow.png"
                    self.click(img)
                    sleep(2)
                self.adb_run([self.adb_cmd, "-s", self.device_id, "shell", "am", "force-stop", "com.linecorp.LGRGS"])
                sleep(3)
                self.open_app()
                sleep(5)
                loop_count = 0
                continue
            
            # === fixok.png Check (เช็คตลอด แต่หยุดหลัง event) ===
            if not event_passed and self.exists_in_cache("img/fixok.png", similarity=0.8):
                print(f"[{self.device_id}] Found fixok.png! Clicking...")
                self.click("img/fixok.png", similarity=0.8)
                sleep(1)
                continue

            # Event / Popups -> กด event แล้วรัว BACK จนเจอ cancel.png หรือ stoplogin.png (Triple Back Mode)
            if self.exists_in_cache("img/event.png"):
                event_passed = True
                print(f"[{self.device_id}] [EVENT] Detected event.png, clicking and starting Triple Back spam...")
                self.click("img/event.png")
                sleep(1)
                
                back_press_count = 0
                while True:
                    # กด Back ทีเดียว 3 รอบ
                    self.adb_shell("input keyevent KEYCODE_BACK")
                    self.adb_shell("input keyevent KEYCODE_BACK")
                    self.adb_shell("input keyevent KEYCODE_BACK")
                    back_press_count += 3
                    print(f"[{self.device_id}] [EVENT] Triple Back spam! (Total: {back_press_count})")
                    
                    sleep(0.3) # ให้เวลา UI อัปเดตเล็กน้อย
                    self.capture_screen()
                    
                    # ถ้าเจอ cancel.png หรือ stoplogin.png ให้หยุด
                    if self.exists_in_cache("img/cancel.png"):
                        print(f"[{self.device_id}] [EVENT] Found cancel.png, clicking...")
                        self.click("img/cancel.png")
                        sleep(1)
                        break
                    
                    if self.exists_in_cache("img/stoplogin.png"):
                        print(f"[{self.device_id}] [EVENT] Found stoplogin.png, breaking loop.")
                        break
                        
                    if back_press_count >= 30: # ป้องกันลูปค้าง (สูงสุด 30 ครั้ง)
                        print(f"[{self.device_id}] [EVENT] Max BACK presses reached (30), continuing...")
                        break
                
                continue
            
            sleep(2)
            if loop_count > 500:
                print(f"[{self.device_id}] Login timeout after 500 iterations")
                status = "timeout"
                return status
        
        return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Ranger+Gear Script v3.2.0")
    parser.add_argument("--device", type=str, help="Specific device ID/address to run (e.g. 127.0.0.1:5557)")
    parser.add_argument("--no-start", action="store_true", help="Don't auto-start bot threads in GUI")
    parser.add_argument("--no-reset-adb", action="store_true", help="Don't kill/start ADB server")
    parser.add_argument("--cli", action="store_true", help="Launch in Command Line mode (no GUI)")
    parser.add_argument("--minimized", action="store_true", help="Minimize window")
    args = parser.parse_args()

    if args.minimized:
        try:
            import ctypes
            # SW_MINIMIZE = 6 or SW_HIDE = 0. Using 2 (SW_SHOWMINIMIZED) or 6.
            # 2 is show minimized, 0 is hide. Let's use 2 as requested "minimized".
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 2)
        except: pass

    print("=== Auto Ranger+Gear Script v3.2.0 ===")
    
    load_config()
    
    # ลบไฟล์ .lock ทั้งหมดตอนเริ่มรัน (ทั้ง backup/ และ temp/)
    cleanup_count = 0
    # 1. ลบ lock เก่าที่อาจค้างใน backup/
    backup_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
    if os.path.exists(backup_folder):
        for lf in glob.glob(os.path.join(backup_folder, "*.lock")):
            try: os.remove(lf); cleanup_count += 1
            except: pass
    # 2. ลบ lock ใน temp/ranger-locks/
    temp_lock_dir = os.path.join(tempfile.gettempdir(), "ranger-locks")
    if os.path.exists(temp_lock_dir):
        for lf in glob.glob(os.path.join(temp_lock_dir, "*.lock")):
            try: os.remove(lf); cleanup_count += 1
            except: pass
    if cleanup_count > 0:
        print(f"[CLEANUP] Removed {cleanup_count} stale .lock file(s)")

    # 3. ลบไฟล์ shared_stats.json เพื่อล้างค่าจากรอบเก่า
    shared_stats_file = ui_stats._get_shared_file()
    if os.path.exists(shared_stats_file):
        try:
            os.remove(shared_stats_file)
            print("[CLEANUP] Removed old shared_stats.json")
        except: pass
    
    # รีเซ็ตค่าในหน่วยความจำด้วย
    ui_stats.success_count = 0
    ui_stats.fail_count = 0
    ui_stats.hero_found_list = {}
    ui_stats.device_statuses = {}
    ui_stats.save_shared()
    
    if not find_adb_executable():
        print("ADB Not Found.")
        sys.exit(1)
    
    # Reset ADB and execute port scan (Skip if requested)
    if not args.no_reset_adb:
        print("[INFO] Connecting to all MuMu ports (ADB Restart inside)...")
        connect_known_ports()
        
    devices = []
    if args.device:
        devices = [args.device]
    else:
        for attempt in range(3):
            devices = get_connected_devices()
            emulator_devices = [d for d in devices if d.startswith("emulator-") or d.startswith("127.0.0.1:")]
            if emulator_devices:
                devices = emulator_devices
                break
            if attempt < 2:
                print(f"[DEV] Attempt {attempt+1}: No devices found yet, waiting 3s...")
                sleep(3)
    
    if not devices:
        print("[ERROR] No devices connected. Make sure your emulator is running.")
        sys.exit(1)

    print(f"[INFO] Connected Devices ({len(devices)}): {', '.join(devices)}")
    
    # Prepare OCR
    find_ranger = config.get("find_ranger", 0)
    find_gear = config.get("find_gear", 0)
    find_all = config.get("find_all", 1)
    if find_gear or find_all:
        print("[INFO] Pre-loading OCR model...")
        try:
            get_ocr_reader()
            print("[OK] OCR model loaded.")
        except Exception as e:
            print(f"[WARN] Failed to load OCR: {e}")
    
    # Setup Queue (Still needed for GUI but threads will use directory scanning)
    source_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
    if os.path.exists(source_folder):
        files = [f for f in os.listdir(source_folder) if f.lower().endswith(".xml")]
        ui_stats.update(total=len(files))
        print(f"[FILE] Found {len(files)} files in {source_folder}")
    
    # Selection
    if not args.cli and GUI_AVAILABLE:
        print(f"{Fore.GREEN}[START] Launching GUI Mode...{Style.RESET_ALL}")
        try:
            ctk.set_appearance_mode("Dark")
            ctk.set_default_color_theme("blue")
            gui = ModernBotGUI(devices, args)
            GUI_INSTANCE = gui
            gui.mainloop()
            sys.exit(0)
        except Exception as e:
            print(f"{Fore.RED}[ERROR] GUI Failed: {e}{Style.RESET_ALL}")
            args.cli = True

    # CLI Mode
    print(f"\n{Fore.CYAN}Starting bot in CLI Mode...{Style.RESET_ALL}")
    
    threads = []
    # If device is specified, only run that one (useful for multi-window mode)
    targets = [args.device] if args.device else devices
    
    print(f"[INFO] Starting {len(targets)} threads...")
    delay = config.get("thread_delay", 5)
    for i, dev in enumerate(targets):
        t = RangerGearBot(dev, args)
        t.start()
        threads.append(t)
        if i < len(targets) - 1:
            sleep(delay)
        
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[STOP] Keyboard Interrupt. Stopping...")
    print("\n[DONE] All tasks completed.")