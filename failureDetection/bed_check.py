"""Is the bed empty? Compare a camera picture with the printer's empty-bed reference (#67).

The reference is taken whenever someone starts a print and confirms "the plate is clear". Later (to decide whether a
printer can take an automatic reprint) a new picture is compared with it:

- only the bed is compared: the calibrated bed outline (#79) when there is one, else the middle of the picture;
- both pictures are shrunk, blurred and brightness-matched per colour channel, so lighting changes and JPEG noise
  don't count, while a part in a colour close to the plate's brightness (red on grey) still does;
- the share of the bed that changed noticeably decides: below `threshold` (0.5%) the bed is clear. On purpose this
  errs towards "parts on the bed": a wrong "parts" only means no automatic start there.

Runs under the system Python (Pillow and numpy from Raspberry Pi OS, installed by setup_ai.py):
    python3 bed_check.py reference.jpg current.jpg '[[u,v],...4 corners]' 0.005
prints {"clear": true/false, "changed": 0.012} or {"error": "..."}.
"""
import json
import sys

SIZE = (192, 108)
PIXEL_CHANGE = 0.12   # a pixel counts as changed when a colour channel differs by more than this (0-1)


def load(path):
    from PIL import Image, ImageFilter
    image = Image.open(path).convert('RGB').resize(SIZE)
    return image.filter(ImageFilter.GaussianBlur(1.5))


def bed_mask(corners):
    """Boolean mask of the bed: the calibrated outline, or the middle of the picture."""
    import numpy as np
    from PIL import Image, ImageDraw
    mask = Image.new('L', SIZE, 0)
    draw = ImageDraw.Draw(mask)
    if corners and len(corners) == 4:
        draw.polygon([(u * SIZE[0], v * SIZE[1]) for u, v in corners], fill=1)
    else:
        draw.rectangle([SIZE[0] * 0.2, SIZE[1] * 0.35, SIZE[0] * 0.8, SIZE[1] * 0.9], fill=1)
    return np.asarray(mask, dtype=bool)


def compare(reference, current, corners=None, threshold=0.005):
    import numpy as np
    a = np.asarray(load(reference), dtype=np.float32) / 255
    b = np.asarray(load(current), dtype=np.float32) / 255
    mask = bed_mask(corners)
    if mask.sum() < 50:
        raise ValueError('The bed area is too small to compare.')
    # Match brightness and contrast per channel on the bed area, so a light switched on or off isn't "a part".
    for channel in range(3):
        a_bed, b_bed = a[..., channel][mask], b[..., channel][mask]
        b[..., channel] = (b[..., channel] - b_bed.mean()) * (a_bed.std() / max(b_bed.std(), 1e-3)) + a_bed.mean()
    changed = float((np.abs(a - b).max(axis=2)[mask] > PIXEL_CHANGE).mean())
    return dict(clear=changed < threshold, changed=round(changed, 4))


def main(argv):
    try:
        corners = json.loads(argv[3]) if len(argv) > 3 and argv[3] else None
        threshold = float(argv[4]) if len(argv) > 4 else 0.005
        print(json.dumps(compare(argv[1], argv[2], corners, threshold)))
    except Exception as exc:
        print(json.dumps({'error': f'{type(exc).__name__}: {exc}'[:300]}))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
