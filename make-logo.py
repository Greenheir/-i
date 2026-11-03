# -*- coding: utf-8 -*-
"""生成 logo.png（署名：明晚一定早睡）—— 打包 exe 时当图标用。

    python make-logo.py            ->  logo.png (512x512)
"""
import os

from PIL import Image, ImageDraw, ImageFont

BRAND = '明晚一定早睡'
BG = (17, 24, 20)          # 深墨绿（和界面顶部条一致）
ACCENT = (215, 255, 62)    # 荧光黄绿
DIM = (125, 138, 132)

FONTS = [r'C:\Windows\Fonts\msyhbd.ttc', r'C:\Windows\Fonts\msyh.ttc',
         r'C:\Windows\Fonts\simhei.ttf']


def load_font(size):
    for p in FONTS:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def fit_font(draw, text, max_w, start=170, floor=24):
    """挑一个能把 text 塞进 max_w 像素的字号（中文字宽差异大，必须实测）。"""
    size = start
    while size > floor:
        f = load_font(size)
        box = draw.textbbox((0, 0), text, font=f)
        if box[2] - box[0] <= max_w:
            return f
        size -= 4
    return load_font(floor)


def main():
    S = 512
    img = Image.new('RGB', (S, S), BG)
    d = ImageDraw.Draw(img)

    # 圆角外框
    d.rounded_rectangle([6, 6, S - 6, S - 6], radius=96, outline=ACCENT, width=8)

    # 署名分两行（正方形图标里 5 个字横排会太小）
    line1, line2 = BRAND[:2], BRAND[2:]
    max_w = S - 120
    # 两行用同一个字号（取较窄的那个），视觉才平衡
    f1 = fit_font(d, line1, max_w)
    f2 = fit_font(d, line2, max_w)
    size = min(f1.size, f2.size)
    font = load_font(size)

    def center(text, y):
        box = d.textbbox((0, 0), text, font=font)
        d.text(((S - (box[2] - box[0])) / 2 - box[0], y), text, font=font, fill=ACCENT)
        return box[3] - box[1]

    h = center(line1, 74)
    center(line2, 74 + h + 26)

    # 底部小字
    fs = load_font(46)
    sub = '口琴工具台'
    box = d.textbbox((0, 0), sub, font=fs)
    d.text(((S - (box[2] - box[0])) / 2 - box[0], S - 108), sub, font=fs, fill=DIM)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')
    img.save(out)
    img.resize((256, 256), Image.LANCZOS).save(out.replace('.png', '-256.png'))
    print('已生成: %s  (署名两行字号 %dpx)' % (out, size))


if __name__ == '__main__':
    main()
