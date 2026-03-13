"""Generate an .ico file for AI Scheduler."""
from PIL import Image, ImageDraw, ImageFont

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Background: rounded-feel dark blue circle (AI/tech vibe)
draw.ellipse([4, 4, SIZE - 4, SIZE - 4], fill="#1A1A2E")

# Accent ring - electric blue
draw.ellipse([14, 14, SIZE - 14, SIZE - 14], outline="#00D4FF", width=3)

# Inner subtle ring
draw.ellipse([22, 22, SIZE - 22, SIZE - 22], outline="#0F3460", width=2)

try:
    font_large = ImageFont.truetype("seguiemj.ttf", 72)
except OSError:
    font_large = ImageFont.load_default()

try:
    font_mid = ImageFont.truetype("arialbd.ttf", 38)
except OSError:
    try:
        font_mid = ImageFont.truetype("arial.ttf", 38)
    except OSError:
        font_mid = ImageFont.load_default()

try:
    font_small = ImageFont.truetype("arialbd.ttf", 24)
except OSError:
    try:
        font_small = ImageFont.truetype("arial.ttf", 24)
    except OSError:
        font_small = ImageFont.load_default()

# Clock/schedule icon (top)
draw.text((SIZE // 2, 60), "🕐", fill="#00D4FF", font=font_large, anchor="mt")

# "AI" text
draw.text((SIZE // 2, 148), "AI", fill="#00D4FF", font=font_mid, anchor="mt")

# "SCHEDULER" text
draw.text((SIZE // 2, 198), "SCHEDULER", fill="white", font=font_small, anchor="mt")

# Save as .ico with multiple sizes
img.save(
    "alert_monitor.ico",
    format="ICO",
    sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
)
print("Icon saved: alert_monitor.ico")
