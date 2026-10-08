"""홈페이지 이미지를 원본에서 만든다 (assets/ → home/img/). 원본을 바꾸면 다시 돌린다.

    python home/make_images.py

DM 캡처에는 실제 멤버의 이름과 프로필 사진이 있어서 흐리게 가린다 (공개 페이지).
"""
import pathlib

from PIL import Image, ImageDraw, ImageFilter

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "assets"
OUT = ROOT / "home" / "img"


def save(im, name, width=None, **kw):
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    path = OUT / name
    im.save(path, **kw)
    print(f"{name}: {im.size[0]}x{im.size[1]}, {path.stat().st_size // 1024} KB")


def blur(im, boxes, radius=9):
    """boxes: (x0, y0, x1, y1) in the original's pixels. Pixelate then blur, so nothing is recoverable."""
    im = im.copy()
    for b in boxes:
        part = im.crop(b)
        small = part.resize((max(1, part.width // 10), max(1, part.height // 10)), Image.BILINEAR)
        part = small.resize(part.size, Image.NEAREST).filter(ImageFilter.GaussianBlur(radius))
        im.paste(part, b[:2])
    return im


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    logo = Image.open(SRC / "design_system/refs/moca_logo_transparent.png").convert("RGBA")
    save(logo, "logo.webp", 720, quality=90, method=6)
    face = logo.crop((450, 360, 870, 780))                     # 모카의 얼굴: 채팅 프로필
    save(face, "avatar.webp", 192, quality=90, method=6)

    real = Image.open(SRC / "design_system/refs/moca_real.png").convert("RGB")
    save(real.crop((270, 120, 1000, 1250)), "moca_real.webp", 720, quality=86, method=6)

    home = Image.open(SRC / "moca_home/screenshot_home.png").convert("RGB")
    save(home, "somoim_home.webp", 607, quality=88, method=6)

    dm = Image.open(SRC / "moca_home/screenshot_dm.png").convert("RGB")
    private = [
        (60, 10, 230, 62),      # 상단 대화 상대 이름
        (0, 615, 62, 690),      # 첫 프로필 사진
        (64, 622, 178, 651),    # 이름 (글자 줄 628–645)
        (0, 990, 62, 1065),     # 두 번째 프로필 사진
        (64, 1004, 178, 1033),  # 이름 (글자 줄 1010–1027)
        (243, 737, 330, 769),   # 모카 메시지 속 '윤서님,' (글자 줄 742–764)
        (243, 1119, 362, 1151), # 모카 메시지 속 '좋아, 윤서!' (글자 줄 1124–1146)
    ]
    save(blur(dm, private), "somoim_dm.webp", 604, quality=88, method=6)

    coffee = Image.open(ROOT / "site" / "moca.png").convert("RGB")          # 지난 활동 카드의 그림
    save(coffee, "coffee_chat.webp", 480, quality=84, method=6)
    save(face.resize((64, 64), Image.LANCZOS), "favicon.png", optimize=True)

    for src, name in (("달력.png", "goods_calendar.jpg"), ("안경닦이.png", "goods_cloth.jpg"), ("키링.png", "goods_keyring.jpg")):
        save(Image.open(SRC / "moca_home" / src).convert("RGB"), name, 720, quality=84, optimize=True, progressive=True)


if __name__ == "__main__":
    main()
