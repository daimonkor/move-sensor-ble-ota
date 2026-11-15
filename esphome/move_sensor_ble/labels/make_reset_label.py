#!/usr/bin/env python3
"""Наклейка Move Sensor BLE (2026-11-15): заводской сброс — переключатель WiFi (SW2) ×5 за 20 с, LED ×6
(одинаковая для всех датчиков). Наклейку с QR точки доступа Дима отменил.
Niimbot D11, лента T15x30 (30x15 мм): 240x120 px @203 dpi, 1-бит, тонкая рамка под обрез.
Текст рисуется со сглаживанием в оттенках серого и потом порогом в 1 бит — так буквы ровнее,
чем при рисовании сразу в 1 бит (там «ть», «SW» ломались).
  python3 make_reset_label.py      -> reset_label.png рядом. Нужен pip: pillow."""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 240, 120
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
THRESH = 150   # серый -> 1 бит: меньше = тоньше буквы

ROWS = [  # текст, размер, отступ после; первая строка — заголовок по центру, дальше список слева
    ("ЗАВОДСКОЙ СБРОС", 18, 6),
    ("1. WiFi вкл/выкл 5 раз", 16, 4),
    ("2. уложиться в 20 с", 16, 4),
    ("3. LED мигнёт 6 раз", 16, 4),
    ("4. WiFi оставить вкл", 16, 0),
]
LIST_X = 10   # отступ пунктов списка слева

img = Image.new("L", (W, H), 255)
d = ImageDraw.Draw(img)
maxw = W - 2 * LIST_X
fonts = []
for t, size, _ in ROWS:
    f = ImageFont.truetype(FONT, size)
    while d.textlength(t, font=f) > maxw and size > 9:
        size -= 1
        f = ImageFont.truetype(FONT, size)
    fonts.append(f)
heights = [d.textbbox((0, 0), t, font=f)[3] - d.textbbox((0, 0), t, font=f)[1] for (t, _, _), f in zip(ROWS, fonts)]
total = sum(heights) + sum(g for _, _, g in ROWS[:-1])
y = (H - total) // 2
for i, ((t, _, g), f, h) in enumerate(zip(ROWS, fonts, heights)):
    top = d.textbbox((0, 0), t, font=f)[1]
    x = (W - d.textlength(t, font=f)) / 2 if i == 0 else LIST_X
    d.text((x, y - top), t, font=f, fill=0)
    y += h + g
if y > H - 3:
    raise SystemExit(f"не влезает по высоте: {y}")
img = img.point(lambda v: 255 if v > THRESH else 0).convert("1")
ImageDraw.Draw(img).rectangle([0, 0, W - 1, H - 1], outline=0)   # линия обреза
out = os.path.join(HERE, "reset_label.png")
img.save(out, dpi=(203, 203))
print(out, "text bottom", y)
