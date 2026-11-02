"""Writes ScreenRec.ico: the tray's red record dot, at the sizes Windows asks for."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle((4 * s, 12 * s, 60 * s, 52 * s), 8 * s, fill=(40, 40, 40, 255))
    d.ellipse((20 * s, 20 * s, 44 * s, 44 * s), fill=(255, 59, 48, 255))
    return img


out = Path(sys.argv[1])
icon(256).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
