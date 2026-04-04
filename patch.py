import re

with open("checkstage.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add GUI_INSTANCE and Imports
import_block = """
import customtkinter as ctk
from datetime import datetime
from tkinter import messagebox

try:
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

GUI_INSTANCE = None
"""

content = content.replace("import math\nfrom scanner import HeroScanner\n", "import math\nfrom scanner import HeroScanner\n" + import_block)

# 2. Add log method to BotInstance
log_method = """
    def log(self, message):
        print(f"[{self.device_id}] {message}")
        if GUI_INSTANCE:
            GUI_INSTANCE.log_to_device(self.device_id, message)

    def load_config(self):
"""
content = content.replace("    def load_config(self):", log_method)

# 3. Replace print with self.log
def repl(m):
    # m.group(1) is the rest of the string inside f"..." starting after [{self.device_id}] \s*
    inner = m.group(1)
    if not inner.strip():
        # Edge case: print(f"[{self.device_id}]")
        return 'self.log("")'
    return f'self.log(f"{inner}")'

# Regex: find print(f"[{self.device_id}] <something>")
# Note: we also have some print(f"[{self.device_id}]   <something>")
content = re.sub(r'print\(f"\[\{self\.device_id\}\]\s*(.*?)"\)', repl, content)

# Replace 'print(f"[{self.device_id}]' (without closing quote)
content = re.sub(r'print\(f"\[\{self\.device_id\}\]\s+', r'self.log(f"', content)

# 4. Insert GUI classes before main()
gui_classes = """
if GUI_AVAILABLE:
    class DeviceLogWindow(ctk.CTkToplevel):
        def __init__(self, parent, device_id):
            super().__init__(parent)
            self.title(f"📑 Logs: {device_id}")
            self.geometry("600x400")
            self.device_id = device_id
            
            self.log_text = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=11), text_color="#d1d5db")
            self.log_text.pack(fill="both", expand=True, padx=10, pady=10)
            self.log_text.configure(state="disabled")
            
            if hasattr(parent, 'device_logs') and device_id in parent.device_logs:
                self.update_logs(parent.device_logs[device_id])
            
            self.after(500, self.auto_refresh)

        def update_logs(self, log_content):
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", log_content)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def auto_refresh(self):
            if not self.winfo_exists(): return
            parent = self.master
            if hasattr(parent, 'device_logs') and self.device_id in parent.device_logs:
                current_text = self.log_text.get("1.0", "end-1c")
                new_text = parent.device_logs[self.device_id]
                if len(new_text) > (len(current_text) + 2):
                    self.update_logs(new_text)
            self.after(1000, self.auto_refresh)

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
        def __init__(self, devices):
            super().__init__()
            global GUI_INSTANCE
            GUI_INSTANCE = self
            
            self.title("CheckStage - Log Monitor")
            self.geometry("450x550")
            self.devices = devices
            self.device_monitors = {}
            self.device_logs = {}
            self.log_windows = {}
            self.bot_threads = []
            self.is_started = False
            
            self.setup_ui()
            self.protocol("WM_DELETE_WINDOW", self.on_closing)
            self.deiconify()
            self.focus_force()

        def setup_ui(self):
            # TOP TOOLBAR
            toolbar = ctk.CTkFrame(self, height=45, fg_color="#333333", corner_radius=0)
            toolbar.pack(fill="x")
            toolbar.pack_propagate(False)
            
            self.lbl_status = ctk.CTkLabel(toolbar, text=f"   ● DEVICES ({len(self.devices)})", font=ctk.CTkFont(size=12, weight="bold"), text_color="#4caf50")
            self.lbl_status.pack(side="left", padx=5)

            self.btn_start = ctk.CTkButton(toolbar, text="▶ START ALL", font=ctk.CTkFont(size=12, weight="bold"), width=100, height=28, fg_color="#e53935", hover_color="#c62828", command=self.start_bot)
            self.btn_start.pack(side="left", padx=10)
            
            self.lbl_auto_start = ctk.CTkLabel(toolbar, text="[ READY ]", font=ctk.CTkFont(size=10, weight="bold"), text_color="#aaaaaa")
            self.lbl_auto_start.pack(side="left", padx=5)
            
            # MAIN CONTENT
            main_frame = ctk.CTkFrame(self, fg_color="transparent")
            main_frame.pack(fill="both", expand=True, padx=6, pady=4)
            
            left_frame = ctk.CTkFrame(main_frame, fg_color="#2b2b2b", corner_radius=8)
            left_frame.pack(fill="both", expand=True)
            
            dev_header = ctk.CTkFrame(left_frame, fg_color="#383838", corner_radius=0, height=28)
            dev_header.pack(fill="x")
            ctk.CTkLabel(dev_header, text="   DEVICES LIST", font=ctk.CTkFont(size=11, weight="bold"), text_color="#cccccc", anchor="w").pack(side="left")
            
            self.dev_scroll = ctk.CTkScrollableFrame(left_frame, fg_color="transparent")
            self.dev_scroll.pack(fill="both", expand=True, padx=3, pady=3)
            for i, dev in enumerate(self.devices):
                m = DeviceMonitorWidget(self, dev, i+1)
                m.pack(fill="x", pady=1)
                self.device_monitors[dev] = m
            
            # LOG AREA (Global)
            log_frame = ctk.CTkFrame(self, fg_color="#1e1e1e", corner_radius=6, height=80)
            log_frame.pack(fill="x", padx=6, pady=(0, 4))
            log_frame.pack_propagate(False)
            
            self.log_text = ctk.CTkTextbox(log_frame, font=ctk.CTkFont(family="Consolas", size=10), text_color="#8b949e", fg_color="#1e1e1e")
            self.log_text.pack(fill="both", expand=True, padx=2, pady=2)
            self.log_text.configure(state="disabled")
            self.log("SYSTEM", "GUI started. Please press [START ALL] to begin.")

        def open_device_log(self, device_id):
            if device_id in self.log_windows and self.log_windows[device_id].winfo_exists():
                self.log_windows[device_id].focus_force()
            else:
                self.log_windows[device_id] = self.DeviceLogWindow(self, device_id)
                self.log_windows[device_id].focus_force()

        def log_to_device(self, device_id, message):
            ts = datetime.now().strftime("%H:%M:%S")
            full_msg = f"[{ts}] {message}\\n"
            if device_id not in self.device_logs:
                self.device_logs[device_id] = ""
            self.device_logs[device_id] += full_msg
            
            lines = self.device_logs[device_id].split('\\n')
            if len(lines) > 500:
                self.device_logs[device_id] = '\\n'.join(lines[-500:])

            if "ERROR" in message or "Exception" in message or "CRASHED" in message or "Finished" in message or ">>>" in message:
                self.log(device_id, message)

        def log(self, level, message): 
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{ts}] {message}\\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def start_bot(self):
            if getattr(self, 'is_started', False): return
            self.is_started = True
            self.btn_start.configure(state="disabled", fg_color="#555555", text="⏳ RUNNING")
            self.lbl_auto_start.configure(text="[ BOT IS RUNNING ]", text_color="#4caf50")
            
            for i, device_id in enumerate(self.devices):
                self.after(i * 3000, lambda d=device_id: self._start_single_bot(d))

        def _start_single_bot(self, device_id):
            bot = BotInstance(device_id)
            t = Thread(target=bot.run_step1)
            t.daemon = True
            t.start()
            self.bot_threads.append(t)
            self.log("INFO", f"🚀 Started bot on {device_id}")

        def on_closing(self):
            if messagebox.askokcancel("Quit", "Stop bot and close?"):
                import sys
                import os
                self.destroy()
                os._exit(0)

def main():
    if not find_adb_executable():
        print("ADB not found.")
        return
    connect_known_ports()
    serials = get_connected_devices()
    if not serials:
        print("No devices found.")
        return

    # Startup Cleanup
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

    if GUI_AVAILABLE:
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        gui = ModernBotGUI(serials)
        gui.mainloop()
    else:
        print("GUI NOT AVAILABLE! Running in standard mode.")
        threads = []
        for serial in serials:
            bot = BotInstance(serial)
            t = Thread(target=bot.run_step1)
            t.daemon = True
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

if __name__ == "__main__":
    main()
"""

# Replace main and put gui classes just before it
content = re.sub(r'def main\(\):.*', gui_classes, content, flags=re.DOTALL)

with open("checkstage_new.py", "w", encoding="utf-8") as f:
    f.write(content)
