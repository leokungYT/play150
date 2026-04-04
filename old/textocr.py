import pytesseract
import cv2
import os
import glob


class Location:
    def __init__(self, x: int, y: int):
        self.x = int(x)
        self.y = int(y)

    def getX(self) -> float:
        return float(self.x)

    def getY(self) -> float:
        return float(self.y)

    def setLocation(self, x: int, y: int):
        self.x = int(x)
        self.y = int(y)

    def offset(self, dx: int, dy: int):
        return Location(self.x + dx, self.y + dy)

    def above(self, dy: int):
        return Location(self.x, self.y - dy)

    def below(self, dy: int):
        return Location(self.x, self.y + dy)

    def left(self, dx: int):
        return Location(self.x - dx, self.y)

    def right(self, dx: int):
        return Location(self.x + dx, self.y)

    def __repr__(self):
        return f"Location(x={self.x}, y={self.y})"
    


class Region:
    def __init__(self, x: int, y: int, w: int, h: int):
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    # ---- Getters ----
    def getX(self): return self.x
    def getY(self): return self.y
    def getW(self): return self.w
    def getH(self): return self.h

    def getTopLeft(self): return Location(self.x, self.y)
    def getTopRight(self): return Location(self.x + self.w, self.y)
    def getBottomLeft(self): return Location(self.x, self.y + self.h)
    def getBottomRight(self): return Location(self.x + self.w, self.y + self.h)
    def getCenter(self): return Location(self.x + self.w // 2, self.y + self.h // 2)
    def getTopCenter(self): return Location(self.x + self.w // 2, self.y)
    def getBottomCenter(self): return Location(self.x + self.w // 2, self.y + self.h)
    def getLeftCenter(self): return Location(self.x, self.y + self.h // 2)
    def getRightCenter(self): return Location(self.x + self.w, self.y + self.h // 2)

    # ---- Setters ----
    def setX(self, number: int): self.x = int(number)
    def setY(self, number: int): self.y = int(number)
    def setW(self, number: int): self.w = int(number)
    def setH(self, number: int): self.h = int(number)

    def __repr__(self):
        return f"Region(x={self.x}, y={self.y}, w={self.w}, h={self.h})"



def textOCR(region:Region, crop=True, psm=6, imageProcessing=True, image_path="screen.png"):
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    import numpy as np

    # อ่านภาพภายใน region (BGR)
    if not os.path.exists(image_path):
        # Try finding any screen*.png if default not found
        screens = glob.glob("screen*.png")
        if screens:
            image_path = screens[0]
            print(f"[WARN] {image_path} not found, using {screens[0]}")
        else:
            print(f"[ERR] Image not found: {image_path}")
            return ""

    full_img = cv2.imread(image_path)
    if full_img is None:
        print(f"[ERR] Could not read image: {image_path}")
        return ""
    img = full_img[region.y:region.y+region.h, region.x:region.x+region.w]

    # Debug: save cropped image
    cv2.imwrite("debug_crop.png", img)
    print(f"[DEBUG] Saved debug_crop.png ({img.shape[1]}x{img.shape[0]})")

    if imageProcessing:
        img = cv2.resize(img, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        white_mask = cv2.inRange(hsv, np.array([0, 0, 180]), np.array([180, 60, 255]))
        yellow_mask = cv2.inRange(hsv, np.array([15, 80, 180]), np.array([40, 255, 255]))
        combined = cv2.bitwise_or(white_mask, yellow_mask)
        
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel)
        combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, kernel)
        
        contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        clean = np.zeros_like(combined)
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = cv2.contourArea(cnt)
            aspect = w / max(h, 1)
            if 10 < h < 80 and 5 < w < 200 and area > 50 and aspect < 10:
                cv2.drawContours(clean, [cnt], -1, 255, -1)
        thresh = cv2.bitwise_not(clean)
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        thresh = gray

    cv2.imwrite("debug_thresh.png", thresh)
    print(f"[DEBUG] Saved debug_thresh.png")

    config = (
        f"--psm {psm} "
        "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789[]' "
        "-c load_system_dawg=0 -c load_freq_dawg=0 "
    )
    text = pytesseract.image_to_string(thresh, lang="eng", config=config)
    return text.strip()


# ==========================================
# EasyOCR - แม่นกว่า Tesseract มาก
# ==========================================
_reader = None

def easyOCR(region:Region, image_path="screen.png", allowlist=None):
    """ระบบ OCR ขั้นเทพ: กรองสีตัวเลขเพื่อความแม่นยำ 100%"""
    global _reader
    import easyocr
    import numpy as np
    
    if _reader is None:
        print("[INFO] Loading EasyOCR model (first time only)...")
        _reader = easyocr.Reader(['en'], gpu=False)
    
    if not os.path.exists(image_path):
        screens = glob.glob("screen*.png")
        if screens: image_path = screens[0]
        else: return ""

    full_img = cv2.imread(image_path)
    if full_img is None: return ""
    img = full_img[region.y:region.y+region.h, region.x:region.x+region.w]
    
    # --- Advanced Preprocessing ---
    # 1. ขยาย 4 เท่า เพื่อแยกรายละเอียด
    img = cv2.resize(img, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    
    # 2. แปลงเป็น HSV เพื่อแยกสี
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    
    # 3. สร้าง Mask เฉพาะสีขาว/เหลือง (สีของตัวเลข)
    # ปรับให้รับสีที่กว้างขึ้นเพื่อความชัวร์
    lower_val = np.array([0, 0, 120])   # ปรับจาก 160 เป็น 120 (รับสีที่มืดลงได้นิดหน่อย)
    upper_val = np.array([180, 100, 255]) 
    mask = cv2.inRange(hsv, lower_val, upper_val)
    
    # 4. Invert ให้เป็นตัวสีดำบนพื้นหลังขาว 
    processed_img = cv2.bitwise_not(mask)
    
    cv2.imwrite("debug_crop.png", processed_img)
    
    # EasyOCR อ่านแบบเน้นตัวเลข
    results = _reader.readtext(processed_img, detail=1, allowlist=allowlist)
    
    texts = []
    for (bbox, text, conf) in results:
        # พยายามเอาเฉพาะตัวเลขออกมา
        import re
        clean_text = "".join(re.findall(r'[0-9sS]+', text))
        print(f"  [{conf:.0%}] RAW: {text} -> CLEAN: {clean_text}")
        if conf > 0.1: # ใช้ Threshold ต่ำได้เพราะภาพสะอาดมากแล้ว
            texts.append(clean_text)
    
    return "\n".join(texts)
