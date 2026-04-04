import cv2
import numpy as np
import easyocr
import re
import os
import threading

# ==========================================
# Singleton OCR Reader (Thread-Safe)
# ==========================================
_ocr_reader = None
_ocr_lock = threading.Lock()

def get_ocr_reader():
    """Get or create EasyOCR reader (singleton) to avoid multiple model loads"""
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_lock:
            if _ocr_reader is None:
                print("[HeroScanner] Booting EasyOCR Model (first time only)...")
                _ocr_reader = easyocr.Reader(['en'], gpu=False)
    return _ocr_reader


class HeroScanner:
    """
    Scanner class for extracting numeric values from game regions (e.g. Hero levels, power).
    Optimized for speed and accuracy using color masking and EasyOCR.
    """
    def __init__(self, device):
        """
        Initialize with a device object.
        device must have a capture_screen() method that returns a BGR image (numpy array).
        """
        self.device = device
        self.reader = get_ocr_reader()

    def find_numbers_in_region(self, region):
        """
        Scans a specific region for numeric values.
        
        Args:
            region: Tuple/List (x, y, w, h)
            
        Returns:
            list of dicts: [
                {"val": 1234, "conf": 0.95, "pos": (absolute_x, absolute_y)},
                ...
            ]
        """
        # 1. Capture screen from device
        img_bgr = self.device.capture_screen()
        if img_bgr is None:
            return []
        
        rx, ry, rw, rh = region
        h_img, w_img = img_bgr.shape[:2]
        
        # 2. Bounds check for cropping
        ry_start = max(0, min(ry, h_img))
        ry_end = max(0, min(ry + rh, h_img))
        rx_start = max(0, min(rx, w_img))
        rx_end = max(0, min(rx + rw, w_img))
        
        if ry_start >= ry_end or rx_start >= rx_end:
            # Region completely outside of image
            return []
            
        crop = img_bgr[ry_start:ry_end, rx_start:rx_end]
        
        # 3. Advanced Preprocessing
        # A. Resize significantly for small text detail (4x scale)
        processed = cv2.resize(crop, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
        
        # B. Color Masking (Isolate white/yellow digits)
        # Convert to HSV to separate color from brightness
        hsv = cv2.cvtColor(processed, cv2.COLOR_BGR2HSV)
        
        # Range for White/Yellowish text (as used in original scripts)
        lower_val = np.array([0, 0, 120])
        upper_val = np.array([180, 100, 255])
        mask = cv2.inRange(hsv, lower_val, upper_val)
        
        # Invert mask: Black text on White background (easier for OCR)
        final_img = cv2.bitwise_not(mask)
        
        # 4. OCR Execution
        try:
            # Use numeric allowlist to minimize errors
            results = self.reader.readtext(final_img, allowlist='0123456789')
        except Exception as e:
            print(f"[HeroScanner] OCR Error: {e}")
            return []
            
        candidates = []
        for (bbox, text, conf) in results:
            # bbox is relative to the SCALED processed image
            # [[tl_x, tl_y], [tr_x, tr_y], [br_x, br_y], [bl_x, bl_y]]
            
            # Match digit sequence
            match = re.search(r'\d+', text)
            if not match:
                continue
                
            try:
                val = int(match.group())
            except ValueError:
                continue
                
            # Calculate absolute screen coordinates
            (tl, tr, br, bl) = bbox
            # cx_rel_crop is the center of the text inside the original crop
            cx_rel_crop = (tl[0] + br[0]) / 2 / 4.0
            cy_rel_crop = (tl[1] + br[1]) / 2 / 4.0
            
            abs_x = int(rx_start + cx_rel_crop)
            abs_y = int(ry_start + cy_rel_crop)
            
            candidates.append({
                "val": val,
                "conf": conf,
                "pos": (abs_x, abs_y)
            })
            
        return candidates

# Self-test logic if run directly
if __name__ == "__main__":
    # Mock device for testing
    class MockDevice:
        def capture_screen(self):
            # Try to load a screenshot if it exists, else return None
            if os.path.exists("screen.png"):
                return cv2.imread("screen.png")
            return None
            
    print("[HeroScanner] Self-test started...")
    scanner = HeroScanner(MockDevice())
    # Test with a dummy region
    res = scanner.find_numbers_in_region((0, 0, 800, 600))
    print(f"[HeroScanner] Test found {len(res)} candidates.")
    for r in res:
        print(f" - {r}")
