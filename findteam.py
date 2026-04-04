import time
import os
from adb_utils import ADBDevice, find_adb, get_devices
from scanner import HeroScanner

# ============================================================
#  CONFIGURATION Region
# ============================================================
REGION_SCAN = (165, 490, 70, 21)   # Region สแกนหาเลข (แถบแนวนอนแคบๆ)
TARGET_VALUE = 1200                  # เลขที่ต่ำกว่านี้จะลาก

# ตำแหน่ง Hero Slot (ด้านบน) — ใช้ลากเคลียร์ + ลากวาง
HERO_SLOTS = [
    (622, 248),   # hero5
    (768, 251),   # hero4
    (447, 179),   # hero3
    (339, 183),   # hero2 
    (196, 188),   # hero1
]


HERO_SLOTSCLear = [
    (767, 175),   # hero5
    (624, 173),   # hero4
    (447, 179),   # hero3
    (339, 183),   # hero2 
    (196, 188),   # hero1
]

# ตำแหน่งปล่อย เมื่อเคลียร์ Hero ออกจากทีม
CLEAR_DROP = (258, 444)

# Image paths
IMG_TEAM      = "img/team.png"
IMG_WAITTEAM  = "img/waitteam.png"
IMG_FILTER1   = "img/filter1.png"
IMG_FILTER2   = "img/filter2.png"
IMG_FILTER3   = "img/filter3.png"
IMG_FILTER4   = "img/filter4.png"
IMG_FILTER5   = "img/filter5.png"
IMG_BACKHERO  = "img/backhero.png"
IMG_SAVETEAM  = "img/saveteam.png"


# Scroll: คลิกค้างขวา -> ปล่อยซ้าย
SCROLL_START = (257, 443)
SCROLL_END   = (4, 440)


def log(device_id, msg):
    print(f"[{device_id}] {msg}")


# ============================================================
#  STEP HELPERS
# ============================================================

def wait_and_click(device, img_path, timeout=30, threshold=0.8):
    """รอจนกว่าจะเจอรูป แล้วคลิก"""
    img_name = os.path.basename(img_path)
    start = time.time()
    while time.time() - start < timeout:
        screen = device.capture_screen()
        if screen is not None:
            result = device.find_image(img_path, threshold)
            if result and result[0] is not None:
                pos, val = result
                log(device.device_id, f"Found {img_name}, clicking at {pos}")
                device.tap(pos[0], pos[1])
                return pos
        time.sleep(0.5)
    log(device.device_id, f"Timeout waiting for {img_name}")
    return None


def wait_for_image(device, img_path, timeout=60, threshold=0.8):
    """รอจนกว่าจะเจอรูป (ไม่คลิก)"""
    img_name = os.path.basename(img_path)
    start = time.time()
    while time.time() - start < timeout:
        screen = device.capture_screen()
        if screen is not None:
            result = device.find_image(img_path, threshold)
            if result and result[0] is not None:
                log(device.device_id, f"Found {img_name}")
                return True
        time.sleep(0.5)
    log(device.device_id, f"Timeout waiting for {img_name}")
    return False




def advanced_drag_hold(device, points, hold_sec=3):
    """
    ลากแบบ 2 จังหวะ: ลากมาวางพัก (ปล่อย) -> กดใหม่ค้างไว้ -> ลากไปเป้าหมาย
    points: [(sx, sy), (mid_x, mid_y), (target_x, target_y)]
    """
    p1, p2, p3 = points
    log(device.device_id, f"  [Double-Drag Hold] Phase 1: {p1}->{p2} (Release)")
    
    # รอบแรก: ลากจากจุดเจอมาปล่อยที่จุดพัก
    device.shell(f"input swipe {p1[0]} {p1[1]} {p2[0]} {p2[1]} 500")
    time.sleep(1.0) # หน่วงเวลาให้เกมรับรู้การวาง
    
    log(device.device_id, f"  [Double-Drag Hold] Phase 2: DragAndDrop {p2} -> {p3} (Focusing Hold)")
    # รอบสอง: ใช้คำสั่ง draganddrop เพื่อให้เครื่องแช่นิ้วไว้ที่ p2 ก่อนลากไปยัง p3
    # หมายเหตุ: 'input draganddrop' จะทำการ Long Press ที่จุดเริ่มให้อัตโนมัติก่อนเริ่มเคลื่อนที่
    device.shell(f"input draganddrop {p2[0]} {p2[1]} {p3[0]} {p3[1]} {hold_sec*1000}")


# ============================================================
#  MAIN FLOW
# ============================================================

def find_team():
    """ขั้นตอนหาทีมหลัก"""
    adb = find_adb()
    if not adb:
        print("ADB not found!")
        return

    devices = get_devices(adb)
    if not devices:
        print("No devices found!")
        return

    dev_id = devices[0]
    device = ADBDevice(dev_id, adb)
    scanner = HeroScanner(device)

    log(dev_id, "=" * 50)
    log(dev_id, "  FIND TEAM - START")
    log(dev_id, "=" * 50)

    # ----------------------------------------------------------
    # STEP 1: กด team
    # ----------------------------------------------------------
    log(dev_id, "STEP 1: Clicking [team]...")
    wait_and_click(device, IMG_TEAM, timeout=60)
    time.sleep(1)

    # ----------------------------------------------------------
    # STEP 2: รอเจอ waitteam ไปเรื่อยๆจนกว่าจะเจอ
    # ----------------------------------------------------------
    log(dev_id, "STEP 2: Waiting for [waitteam]...")
    while True:
        if wait_for_image(device, IMG_WAITTEAM, timeout=5):
            log(dev_id, "waitteam found! Proceeding...")
            break
        log(dev_id, "waitteam not found yet, retrying...")
        time.sleep(1)
    time.sleep(1)

    # ----------------------------------------------------------
    # STEP 3: ลากเคลียร์ hero ทั้ง 5 ตำแหน่ง
    # ----------------------------------------------------------
    log(dev_id, "STEP 3: Clearing all hero slots...")
    for i, slot in enumerate(HERO_SLOTSCLear):
        log(dev_id, f"  Clearing hero{i+1}: ({slot[0]}, {slot[1]}) -> ({CLEAR_DROP[0]}, {CLEAR_DROP[1]})")
        device.drag_and_drop(slot[0], slot[1], CLEAR_DROP[0], CLEAR_DROP[1], duration=500)
        time.sleep(0.3)
    time.sleep(0.5)

    # ----------------------------------------------------------
    # STEP 4: กด filter1 -> filter2 -> filter3 -> filter4 -> filter5
    # ----------------------------------------------------------
    log(dev_id, "STEP 4: Applying filters...")
    filters = [IMG_FILTER1, IMG_FILTER2, IMG_FILTER3, IMG_FILTER4, IMG_FILTER5]
    for f_img in filters:
        f_name = os.path.basename(f_img)
        log(dev_id, f"  Clicking {f_name}...")
        pos = wait_and_click(device, f_img, timeout=15)
        if pos:
            time.sleep(0.3)
        else:
            log(dev_id, f"  WARNING: {f_name} not found, continuing anyway...")
    time.sleep(0.5)

    # ----------------------------------------------------------
    # STEP 5: สแกนหาเลข < 1200 แล้วลากไปวาง
    #   - สแกน region -> หาเลข < 1200 (conf >= 0.5, val >= 100)
    #   - ลาก 1 ตัว -> สแกนใหม่
    #   - ถ้าไม่เจอ -> เลื่อน 1 รอบ -> สแกนใหม่
    #   - วนจนครบ 5 ตำแหน่ง
    # ----------------------------------------------------------
    log(dev_id, "STEP 5: Scanning and dragging heroes...")

    filled_count = 0        # จำนวน slot ที่ลากไปแล้ว
    max_scroll_attempts = 20
    scroll_count = 0

    while filled_count < 5 and scroll_count <= max_scroll_attempts:
        # สแกนหาเลขใน region
        log(dev_id, f"  Scanning region {REGION_SCAN}...")
        candidates = scanner.find_numbers_in_region(REGION_SCAN)

        # กรองเฉพาะเลข < TARGET_VALUE, conf >= 0.5, val >= 100 (ตัดขยะ)
        under_target = []
        for c in candidates:
            if c["val"] < TARGET_VALUE and c["val"] >= 100 and c["conf"] >= 0.5:
                under_target.append(c)

        if under_target:
            hero_info = [f"{c['val']}(conf:{c['conf']:.2f})" for c in under_target]
            log(dev_id, f"  Found valid hero: {hero_info}")

            # ลากตัวที่เจอในกรอบนี้ (ปกติจะมีตัวเดียว)
            for hero in under_target:
                if filled_count >= 5:
                    break

                target_slot = HERO_SLOTS[filled_count]
                sx, sy = hero["pos"]

                log(dev_id, f"  Dragging hero {hero['val']} from ({sx}, {sy}) -> hero{filled_count+1} {target_slot}")
                
                # เช็คว่าเป็น hero4 (768, 251) หรือ hero5 (622, 248) หรือไม่ (ใช้ Non-Stop Hold Drag)
                if target_slot in [(622, 248), (768, 251)]:
                    advanced_drag_hold(device, [(sx, sy), (486, 189), target_slot], hold_sec=3)
                else:
                    # Slot อื่นๆ (hero1-3) ลากแบบปกติ
                    device.drag_and_drop(sx, sy, target_slot[0], target_slot[1], duration=500)

                time.sleep(5)
                filled_count += 1
                log(dev_id, f"  Slots filled: {filled_count}/5")

        # --- Always Scroll: ไม่ว่าจะเจอหรือไม่เจอ ให้เลื่อนหน้าจอเพื่อรอตัวถัดไป ---
        if filled_count < 5:
            scroll_count += 1
            log(dev_id, f"  Scrolling to next candidate (attempt {scroll_count}/{max_scroll_attempts})...")
            device.swipe(SCROLL_START[0], SCROLL_START[1], SCROLL_END[0], SCROLL_END[1], duration=1000)
            time.sleep(3)
        else:
            log(dev_id, "Find team complete, stopping scan.")
            break

    if filled_count >= 5:
        log(dev_id, "All 5 hero slots filled!")
    else:
        log(dev_id, f"Could only fill {filled_count}/5 slots after {scroll_count} scrolls.")

    # ----------------------------------------------------------
    # STEP 6: กด backhero -> saveteam
    # ----------------------------------------------------------
    log(dev_id, "STEP 6: Saving team...")
    time.sleep(1)

    log(dev_id, "  Clicking [backhero]...")
    wait_and_click(device, IMG_BACKHERO, timeout=15)
    time.sleep(1)

    log(dev_id, "  Clicking [saveteam]...")
    wait_and_click(device, IMG_SAVETEAM, timeout=15)
    time.sleep(1)

    log(dev_id, "=" * 50)
    log(dev_id, "  FIND TEAM - COMPLETED!")
    log(dev_id, "=" * 50)


if __name__ == "__main__":
    find_team()
