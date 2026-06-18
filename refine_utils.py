"""Slim refinement helpers — bundled with WebTracker.exe.

Adapted from PC-Agent/web_postprocess/utils.py but with the numpy
dependency removed (uses PIL.ImageChops for pixel comparison).
"""

import json
import os

from PIL import Image, ImageChops, ImageDraw


def load_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def save_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def get_action_type(entry):
    a = entry.get("action") or {}
    if isinstance(a, dict):
        return a.get("action")
    return None


def get_click_coord(entry):
    a = entry.get("action") or {}
    if not isinstance(a, dict):
        return None
    if a.get("action") == "left_click":
        coord = a.get("coordinate")
        if coord and len(coord) == 2:
            return tuple(coord)
    return None


POINT_RADIUS = 4
CIRCLE_RADIUS = 18
CIRCLE_WIDTH = 3


def mark_click_point(image_path, x, y, save_path=None):
    """Draw a red dot + ring at (x, y). Returns saved path."""
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.ellipse(
            [(x - POINT_RADIUS, y - POINT_RADIUS),
             (x + POINT_RADIUS, y + POINT_RADIUS)],
            fill="red",
        )
        draw.ellipse(
            [(x - CIRCLE_RADIUS, y - CIRCLE_RADIUS),
             (x + CIRCLE_RADIUS, y + CIRCLE_RADIUS)],
            outline="red",
            width=CIRCLE_WIDTH,
        )
        if save_path is None:
            base, ext = os.path.splitext(image_path)
            save_path = f"{base}_marked{ext}"
        img.save(save_path)
    return save_path


def are_images_identical(path1, path2):
    """Pixel-exact comparison using PIL only (no numpy)."""
    try:
        with Image.open(path1) as a, Image.open(path2) as b:
            a = a.convert("RGB")
            b = b.convert("RGB")
            if a.size != b.size:
                return False
            return ImageChops.difference(a, b).getbbox() is None
    except Exception:
        return False
