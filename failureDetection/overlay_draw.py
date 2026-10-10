"""AI boxes drawn onto a camera picture, for the pictures that leave the dashboard as plain images (Discord /printer
and notifications, Take camera snapshot, the raw live stream). The dashboard's own views draw the same boxes over the
picture in the browser instead (static/controls.js), so they can be switched off.

Same colours as the dashboard: red for a failure at or above the failure score, yellow at or above the keep-counting
score, faint yellow for a weaker failure class, blue for a class that isn't a failure (such as `print`).

Runs in the service's Python: Pillow comes with bambulabs-api.
"""
import io

RED, YELLOW, FAINT, BLUE = (255, 92, 82), (243, 214, 132), (190, 170, 110), (141, 198, 255)


def colour(box, overlay):
    if not box.get('counts'):
        return BLUE
    if box['score'] >= overlay.get('threshold', 1):
        return RED
    return YELLOW if box['score'] >= overlay.get('hold', 1) else FAINT


def font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)   # Pillow 10.1+
    except TypeError:
        return ImageFont.load_default()


def draw(jpeg, overlay):
    """The picture with the overlay's boxes (0-1 fractions of the picture) and a small "AI" note, as JPEG bytes."""
    from PIL import Image, ImageDraw
    image = Image.open(io.BytesIO(jpeg)).convert('RGB')
    pen, w, h = ImageDraw.Draw(image), image.width, image.height
    unit = max(1, round(min(w, h) / 360))
    label_font = font(13 * unit)
    for box in overlay.get('boxes') or []:
        x0, y0, x1, y1 = (round(v * size) for v, size in zip(box['box'], (w, h, w, h)))
        if x1 <= x0 or y1 <= y0:
            continue
        strong = colour(box, overlay) == RED
        pen.rectangle([x0, y0, x1, y1], outline=colour(box, overlay), width=(3 if strong else 2) * unit)
        text = f"{box.get('label', '?')} {round(box['score'] * 100)}%"
        left, top, right, bottom = pen.textbbox((0, 0), text, font=label_font)
        tw, th = right - left + 8 * unit, bottom - top + 6 * unit
        ty = max(0, y0 - th)
        pen.rectangle([x0, ty, x0 + tw, ty + th], fill=colour(box, overlay))
        pen.text((x0 + 4 * unit, ty + 3 * unit - top), text, fill=(17, 17, 17), font=label_font)
    note = 'AI' + ('' if overlay.get('boxes') else ' · nothing found')
    note_font = font(11 * unit)
    left, top, right, bottom = pen.textbbox((0, 0), note, font=note_font)
    nw, nh = right - left + 10 * unit, bottom - top + 6 * unit
    pen.rectangle([w - nw - 6 * unit, h - nh - 6 * unit, w - 6 * unit, h - 6 * unit], fill=(0, 0, 0))
    pen.text((w - nw - unit, h - nh - 3 * unit - top), note, fill=(255, 255, 255), font=note_font)
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=85)
    return out.getvalue()
