#!/usr/bin/env python3
"""Наклейка Move Sensor BLE (2026-11-15): заводской сброс — переключатель WiFi (SW2) ×5 за 20 с, LED ×6
(одинаковая для всех датчиков). Наклейку с QR точки доступа Дима отменил.
Niimbot D11, лента T15x30 (30x15 мм): 240x120 px @203 dpi, 1-бит, без рамки (2026-11-15: рамку убрали по просьбе Димы).
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
    ("1. начать с WiFi выкл", 16, 4),
    ("2. 5 раз вкл за 20 с", 16, 4),
    ("3. 5-й раз не выключать", 16, 4),
    ("4. LED мигнёт 6 раз", 16, 0),
]
LIST_X = 6   # отступ пунктов списка слева

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
# ровный шаг строк: высота по метрикам шрифта (от верха заглавных до низа «р/у»), а не по рамке
# конкретного текста — иначе строки без выносных букв «прилипали» к следующей
def cap_top(f): return d.textbbox((0, 0), "Н", font=f)[1]
heights = [f.getmetrics()[0] + f.getmetrics()[1] - cap_top(f) for f in fonts]
total = sum(heights) + sum(g for _, _, g in ROWS[:-1])
y = (H - total) // 2
for i, ((t, _, g), f, h) in enumerate(zip(ROWS, fonts, heights)):
    top = cap_top(f)
    x = (W - d.textlength(t, font=f)) / 2 if i == 0 else LIST_X
    d.text((x, y - top), t, font=f, fill=0)
    y += h + g
if y > H - 3:
    raise SystemExit(f"не влезает по высоте: {y}")
img = img.point(lambda v: 255 if v > THRESH else 0).convert("1")
out = os.path.join(HERE, "reset_label.png")
img.save(out, dpi=(203, 203))
print(out, "text bottom", y)
