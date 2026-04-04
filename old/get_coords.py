"""Click on image to get coordinates - for finding Region values"""
import cv2
import subprocess
import os

device_id = "emulator-5556"
filename = "screen_coord.png"

# Capture screen
print("Capturing screen...")
os.system(fr'adb\adb -s {device_id} exec-out screencap -p > {filename}')

if not os.path.exists(filename):
    print("Failed to capture screen!")
    exit()

img = cv2.imread(filename)
clicks = []

def mouse_callback(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        clicks.append((x, y))
        print(f"Click #{len(clicks)}: ({x}, {y})")
        
        # Draw circle
        cv2.circle(img, (x, y), 5, (0, 0, 255), -1)
        cv2.putText(img, f"({x},{y})", (x+10, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        
        if len(clicks) == 2:
            x1, y1 = clicks[0]
            x2, y2 = clicks[1]
            w = x2 - x1
            h = y2 - y1
            print(f"\n===== RESULT =====")
            print(f"Top-Left:     ({x1}, {y1})")
            print(f"Bottom-Right: ({x2}, {y2})")
            print(f"Region({x1}, {y1}, {w}, {h})")
            print(f"==================")
            
            # Draw rectangle
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        
        cv2.imshow("Click to get coordinates (ESC to exit)", img)

cv2.imshow("Click to get coordinates (ESC to exit)", img)
cv2.setMouseCallback("Click to get coordinates (ESC to exit)", mouse_callback)

print("Click TOP-LEFT corner first, then BOTTOM-RIGHT corner")
print("Press ESC to exit")

while True:
    key = cv2.waitKey(1)
    if key == 27:  # ESC
        break

cv2.destroyAllWindows()
