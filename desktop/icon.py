"""The app's icon: the dashboard's green "3D" mark. In the tray it gets a coloured dot when something needs a look,
like the dashboard's own status icon (red error, yellow warning, blue working, grey not connected)."""
from PIL import Image, ImageDraw, ImageFont

GREEN, INK, RING = (186, 247, 120, 255), (21, 32, 14, 255), (19, 25, 22, 255)
DOTS = {'error': (255, 159, 153, 255), 'warn': (243, 214, 132, 255), 'busy': (141, 198, 255, 255),
        'offline': (157, 171, 163, 255)}
FONTS = ('segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf')
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def font(size):
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def draw(size=64, dot=None):
    """The icon as a square RGBA image, drawn large and scaled down so the edges stay smooth."""
    big = max(size, 64) * 4
    image = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 4, fill=GREEN)
    pen.text((big / 2, big * 0.52), '3D', font=font(int(big * 0.52)), fill=INK, anchor='mm')
    if dot in DOTS:
        radius = big * 0.2
        centre = big - radius - big * 0.03
        pen.ellipse((centre - radius * 1.3, centre - radius * 1.3, centre + radius * 1.3, centre + radius * 1.3), fill=RING)
        pen.ellipse((centre - radius, centre - radius, centre + radius, centre + radius), fill=DOTS[dot])
    return image.resize((size, size), Image.LANCZOS)


def write_ico(path):
    """The .exe's icon (desktop/build.py), with every size Windows asks for."""
    draw(256).save(path, format='ICO', sizes=[(s, s) for s in ICO_SIZES])
