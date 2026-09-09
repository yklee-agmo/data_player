"""data-player 아이콘 생성: 필름 스트립 + 재생 삼각형."""
import numpy as np
from PIL import Image, ImageDraw

S = 1024           # 4x 슈퍼샘플링용 큰 캔버스
R = 224            # 모서리 반경

# --- 배경 그라디언트 -------------------------------------------------------
top, bottom = np.array([58, 74, 96]), np.array([28, 35, 46])
grad = (top + (bottom - top) * (np.arange(S)[:, None] / (S - 1)))
bg = np.repeat(grad[:, None, :], S, axis=1).astype(np.uint8)
img = Image.fromarray(bg, "RGB").convert("RGBA")

# 둥근 사각형 마스크
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=R, fill=255)
img.putalpha(mask)

d = ImageDraw.Draw(img)

# --- 필름 스트립 (양쪽 세로 띠 + 구멍) --------------------------------------
strip_w = 132
for x0 in (0, S - strip_w):
    d.rectangle([x0, 0, x0 + strip_w, S], fill=(20, 26, 34, 255))
hole_w, hole_h = 62, 78
for i in range(4):
    cy = 150 + i * 244
    for cx in (66, S - 66):
        d.rounded_rectangle(
            [cx - hole_w // 2, cy - hole_h // 2, cx + hole_w // 2, cy + hole_h // 2],
            radius=20, fill=(232, 237, 242, 255))
# 스트립 안쪽 경계선
for x in (strip_w, S - strip_w):
    d.line([(x, 0), (x, S)], fill=(12, 16, 22, 255), width=6)

# --- 재생 삼각형 -----------------------------------------------------------
cx, cy, r = S // 2 - 10, S // 2, 250
tri = [(cx - r * 0.62, cy - r * 0.86), (cx - r * 0.62, cy + r * 0.86), (cx + r * 0.80, cy)]
d.polygon([(int(x), int(y)) for x, y in tri], fill=(255, 196, 77, 255))

# 둥근 사각형 밖으로 삐져나온 부분 정리
img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (S, S), 0), mask))

# --- 저장 ------------------------------------------------------------------
for size in (16, 24, 32, 48, 64, 128, 256, 512):
    img.resize((size, size), Image.LANCZOS).save(f"assets/icon_{size}.png")
img.resize((256, 256), Image.LANCZOS).save("assets/data-player.png")
print("icons written")
