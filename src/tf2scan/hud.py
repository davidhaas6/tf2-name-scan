import numpy as np
from PIL import Image, ImageDraw, ImageOps


def boxes(size, profile):
    width, height = size
    roi = profile["roi"]
    x, y = round(roi["x"] * width), round(roi["y"] * height)
    w, h = round(roi["width"] * width), round(roi["height"] * height)
    padding = profile.get("padding", {})
    rows = []
    for i in range(profile["row_count"]):
        top = y + round(i * profile["row_step"] * h)
        rows.append(
            (
                max(0, x - padding.get("left", 0)),
                max(0, top - padding.get("top", 0)),
                min(width, x + w + padding.get("right", 0)),
                min(height, top + round(profile["row_height"] * h) + padding.get("bottom", 0)),
            )
        )
    return (x, y, x + w, y + h), rows


def useful(crop, min_contrast=4, min_edge_density=0.01):
    gray = np.asarray(crop.convert("L"), dtype=np.float32)
    if min(gray.shape) < 2:
        return False
    edge = np.mean(np.abs(np.diff(gray, axis=1)) > 12)
    return float(gray.std()) >= min_contrast and edge >= min_edge_density


def prepare(crop, settings):
    mode = settings.get("preprocessing", "rgb")
    if mode == "grayscale":
        crop = ImageOps.grayscale(crop).convert("RGB")
    elif mode == "contrast":
        crop = ImageOps.autocontrast(crop)
    scale = settings.get("upscale", 2)
    resample = getattr(Image.Resampling, settings.get("interpolation", "bicubic").upper())
    return crop.resize(
        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), resample
    )


def annotate(frame, profile):
    image = frame.copy()
    draw = ImageDraw.Draw(image)
    roi, rows = boxes(image.size, profile)
    draw.rectangle(roi, outline="red", width=3)
    for i, box in enumerate(rows):
        draw.rectangle(box, outline="lime", width=2)
        draw.text((box[0], box[1]), str(i), fill="yellow")
    return image
