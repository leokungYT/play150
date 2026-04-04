import cv2
import numpy as np
import subprocess
import os
import ssl
from time import sleep
import pytesseract
from textocr import * 

# Fix SSL certificate error for downloading EasyOCR models
ssl._create_default_https_context = ssl._create_unverified_context

device_id = "emulator-5556"
filename = "screen-5556.png"


def get_device_id():
    # Auto-connect MuMu ports
    for port in range(5555, 5600, 2):
        try:
            subprocess.run([r"adb\adb", "connect", f"127.0.0.1:{port}"],
                          capture_output=True, timeout=2)
        except:
            pass
    sleep(1)
    
    result = subprocess.check_output(r"adb\adb devices", shell=True).decode()
    print(result)
    lines = result.strip().split("\n")[1:]
    devices = [line.split()[0] for line in lines if "device" in line and not line.startswith("*")]
    if not devices:
        print("ERROR: No devices found! Make sure emulator is running.")
        input("Press Enter to exit...")
        exit(1)
    return devices[0]

def capture_screen():
    os.system(fr"adb\adb -s {device_id} exec-out screencap -p > {filename}")
    if not os.path.exists(filename):
        raise Exception("❌ จับภาพหน้าจอไม่สำเร็จ")

def find(template_path, similarity=0.8):
    capture_screen()
    img = cv2.imread(filename, 0)
    template = cv2.imread(template_path, 0)

    if img is None or template is None:
        raise Exception(f"❌ ไม่พบภาพหรือเทมเพลต: {template_path}")

    result = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)
    loc = np.where(result >= similarity)
    if len(loc[0]) > 0:
        y, x = loc[0][0], loc[1][0]
        h, w = template.shape
        return x+w//2, y+h//2
    return None

def exists(template_path, similarity=0.8):
    return find(template_path, similarity) is not None

def click(PSMRL, similarity=0.8):
    target = None
    
    if isinstance(PSMRL, str) and os.path.exists(PSMRL):  
        # pattern image path
        target = find(PSMRL, similarity)
    elif isinstance(PSMRL, tuple) and len(PSMRL)==2:
        # (x,y)
        target = PSMRL

    if target:
        x, y = target
        subprocess.run([
            r"adb\adb", "-s", device_id, "shell",
            "input", "tap", str(x), str(y)
        ])
        return 1
    else:
        print("❌ Click fail, no target")
        return 0

def getColor(region):
    capture_screen()
    image = cv2.imread(filename, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("No image set in Region")
    
    if isinstance(region, tuple) and len(region)==2:
        # (x,y)
        cx, cy = region

    b, g, r = image[cy, cx]

    return (int(r), int(g), int(b))

def swipe(start_x, start_y, end_x, end_y, duration_ms=300):
    os.system(
        fr'adb\adb -s {device_id} shell input swipe {start_x} {start_y} {end_x} {end_y} {duration_ms}')


if __name__ == "__main__":
    device_id = get_device_id()

    # # click((r"img\icon.png"))

    # capture_screen()
    # # print(find(r"img\icon.png"))
    # # print(exists(r"img\icon.png"))

    # # if exists(r"img\icon.png"):
    # #     click(r"img\icon.png")
    # # else:
    # #     click("Not found")

    capture_screen()
    
    # ใช้ EasyOCR อ่าน grid ทั้งหมดทีเดียว
    # ใช้ EasyOCR อ่านเลขด่าน
    result = easyOCR(Region(208, 204, 153, 123), image_path=filename, allowlist='0123456789sS')
    # result = easyOCR(Region(324, 232, 168, 106), image_path=filename, allowlist='0123456789sS')
    print("===== OCR Result =====")
    print(result)
    print("======================")
