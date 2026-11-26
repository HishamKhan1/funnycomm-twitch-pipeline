"""Graphics: headline, attribution tag, ranking list, number slams, thumbnails."""
from __future__ import annotations
import re
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1080, 1920
GREEN = (43, 227, 143)
INK = (9, 13, 20)
GOLD, SILVER, BRONZE = (255, 201, 60), (214, 222, 232), (232, 148, 88)
FONTS = 'C:/Windows/Fonts/'
_EMOJI = re.compile('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]')


@lru_cache(maxsize=64)
def font(size, kind='black'):
    names = {'black': ['seguibl.ttf', 'ariblk.ttf', 'arialbd.ttf'], 'bold': ['segoeuib.ttf', 'arialbd.ttf'],
             'impact': ['impact.ttf', 'seguibl.ttf', 'arialbd.ttf'], 'emoji': ['seguiemj.ttf'], 'semi': ['seguisb.ttf', 'segoeuib.ttf', 'arialbd.ttf']}[kind]
    for n in names:
        if Path(FONTS + n).exists():
            return ImageFont.truetype(FONTS + n, size)
    for n in ('DejaVuSans-Bold.ttf',):
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _runs(text):
    """Split into (is_emoji, chunk)."""
    out, cur, flag = [], '', None
    for ch in text:
        if ch in '\ufe0f\u200d':
            continue
        e = bool(_EMOJI.match(ch))
        if flag is None or e == flag:
            cur += ch
        else:
            out.append((flag, cur))
            cur = ch
        flag = e
    if cur:
        out.append((flag, cur))
    return out


def text_width(d, text, f, size):
    return sum(d.textlength(c, font=font(size, 'emoji') if e else f) for e, c in _runs(text))


def put(d, xy, text, f, size, fill, stroke=0, stroke_fill=(0, 0, 0), emoji=True):
    x, y = xy
    for e, c in _runs(text):
        if e:
            ef = font(size, 'emoji')
            if emoji:
                d.text((x, y + size * .12), c, font=ef, embedded_color=True)
            x += d.textlength(c, font=ef)
        else:
            d.text((x, y), c, font=f, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
            x += d.textlength(c, font=f)
    return x


def wrap(d, text, f, size, width, max_lines):
    words, lines, cur = [], [], ''
    for w in text.split():               # emoji stay glued to the word before them
        if words and _EMOJI.match(w):
            words[-1] += ' ' + w
        else:
            words.append(w)
    for w in words:
        t = (cur + ' ' + w).strip()
        if text_width(d, t, f, size) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        return None
    if len(lines) == 2:                   # balance so the second line is not an orphan
        best, wl = lines, max(text_width(d, x, f, size) for x in lines)
        for k in range(1, len(words)):
            pair = [' '.join(words[:k]), ' '.join(words[k:])]
            m = max(text_width(d, x, f, size) for x in pair)
            if m < wl:
                best, wl = pair, m
        return best
    return lines


def fit_text(text, width, max_lines, sizes, kind='black'):
    d = ImageDraw.Draw(Image.new('RGBA', (8, 8)))
    for s in sizes:
        f = font(s, kind)
        ln = wrap(d, text, f, s, width, max_lines)
        if ln and all(text_width(d, x, f, s) <= width for x in ln):
            return ln, s
    s = sizes[-1]
    f = font(s, kind)
    words = text.split()
    while words:
        words.pop()
        ln = wrap(d, ' '.join(words) + '…', f, s, width, max_lines)
        if ln:
            return ln, s
    return [text[:18]], s


def headline(text, width=960, max_lines=2):
    """Bold stroked headline on a soft shadow. RGBA numpy (premultiplied at blend time)."""
    lines, size = fit_text(text, width, max_lines, [86, 78, 70, 62, 54, 48])
    f = font(size)
    pad, lh = 40, int(size * 1.16)
    im = Image.new('RGBA', (width + pad * 2, lh * len(lines) + pad * 2), (0, 0, 0, 0))
    sh = Image.new('RGBA', im.size, (0, 0, 0, 0))
    d, ds = ImageDraw.Draw(im), ImageDraw.Draw(sh)
    for i, ln in enumerate(lines):
        w = text_width(d, ln, f, size)
        x, y = pad + (width - w) / 2, pad + i * lh - size * .1
        put(ds, (x, y + 7), ln, f, size, (0, 0, 0, 190), stroke=12, stroke_fill=(0, 0, 0, 190))
    sh = sh.filter(ImageFilter.GaussianBlur(8))
    im = Image.alpha_composite(sh, im)
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        w = text_width(d, ln, f, size)
        x, y = pad + (width - w) / 2, pad + i * lh - size * .1
        put(d, (x, y), ln, f, size, (255, 255, 255, 255), stroke=max(5, size // 11), stroke_fill=(0, 0, 0, 255))
    return np.array(im)


def caption(text, width=860, max_lines=2):
    """Plain, hand-typed looking caption: sentence case, clean white on a soft dark plate."""
    lines, size = fit_text(text, width, max_lines, [62, 56, 50, 45], 'bold')
    f = font(size, 'bold')
    d0 = ImageDraw.Draw(Image.new('RGBA', (8, 8)))
    lh, px, py = int(size * 1.22), 34, 20
    w = int(max(text_width(d0, x, f, size) for x in lines)) + px * 2
    h = lh * len(lines) + py * 2
    im = Image.new('RGBA', (w + 24, h + 24), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((12, 12, w + 11, h + 11), 26, fill=(0, 0, 0, 168))
    for i, ln in enumerate(lines):
        tw = text_width(d, ln, f, size)
        put(d, (12 + (w - tw) / 2, 12 + py + i * lh - size * .10), ln, f, size, (255, 255, 255, 255))
    return np.array(im)


def subline(text, width=920, max_lines=2):
    """The line that stays on screen: plain white sentence case with its emoji, a soft shadow, no box."""
    lines, size = fit_text(text, width, max_lines, [58, 54, 50, 46, 42], 'semi')
    f = font(size, 'semi')
    pad, lh = 26, int(size * 1.24)
    im = Image.new('RGBA', (width + pad * 2, lh * len(lines) + pad * 2), (0, 0, 0, 0))
    sh = Image.new('RGBA', im.size, (0, 0, 0, 0))
    d0, ds = ImageDraw.Draw(im), ImageDraw.Draw(sh)
    spots = []
    for i, ln in enumerate(lines):
        w = text_width(d0, ln, f, size)
        spots.append((pad + (width - w) / 2, pad + i * lh - size * .08))
        put(ds, (spots[-1][0], spots[-1][1] + 3), ln, f, size, (0, 0, 0, 210), stroke=5, stroke_fill=(0, 0, 0, 210), emoji=False)
    im = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(6)), im)
    d = ImageDraw.Draw(im)
    for ln, xy in zip(lines, spots):
        put(d, xy, ln, f, size, (255, 255, 255, 255), stroke=2, stroke_fill=(0, 0, 0, 170))
    return np.array(im)


def tag(name, brand='FUNNYCOMM'):
    """Small attribution pill: channel brand + streamer."""
    f1, f2 = font(25, 'black'), font(29, 'bold')
    d = ImageDraw.Draw(Image.new('RGBA', (8, 8)))
    a = d.textlength(brand, font=f1)
    txt = name.upper()
    b = d.textlength(txt, font=f2)
    w = int(a + b + 78)
    im = Image.new('RGBA', (w, 56), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, 55), 28, fill=(*INK, 205))
    d.rounded_rectangle((5, 5, int(a) + 37, 50), 23, fill=(*GREEN, 255))
    d.text((21, 10), brand, font=f1, fill=INK)
    d.text((int(a) + 52, 8), txt, font=f2, fill=(255, 255, 255, 255))
    return np.array(im)


def rank_color(n):
    return {1: GOLD, 2: SILVER, 3: BRONZE}.get(n, (150, 160, 176))


def rank_list(labels, active):
    """labels: [(number, text or '')] in number order; text blank until revealed."""
    f_n, f_t = font(54, 'black'), font(40, 'black')
    lh = 78
    w = 700
    im = Image.new('RGBA', (w, lh * len(labels) + 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, (num, text) in enumerate(labels):
        y = 8 + i * lh
        col = rank_color(num)
        on = num == active
        put(d, (14, y - 4), f'{num}.', f_n, 54, (*col, 255 if text or on else 190), stroke=6, stroke_fill=(0, 0, 0, 255))
        if text:
            t = text
            while text_width(d, t, f_t, 40) > w - 120 and len(t) > 4:
                t = t[:-2].rstrip() + '…'
            put(d, (92, y + 8), t, f_t, 40, (255, 255, 255, 255) if on else (206, 212, 220, 225), stroke=6, stroke_fill=(0, 0, 0, 255))
    return np.array(im)


def number_slam(n):
    size = 330
    f = font(size, 'black')
    im = Image.new('RGBA', (760, 520), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    t = f'#{n}'
    w = d.textlength(t, font=f)
    sh = Image.new('RGBA', im.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).text(((760 - w) / 2, 30), t, font=f, fill=(0, 0, 0, 210), stroke_width=26, stroke_fill=(0, 0, 0, 210))
    im = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)), im)
    ImageDraw.Draw(im).text(((760 - w) / 2, 22), t, font=f, fill=(*rank_color(n), 255), stroke_width=18, stroke_fill=(0, 0, 0, 255))
    return np.array(im)


def chip(text, fill=(230, 50, 60)):
    f = font(40, 'black')
    d = ImageDraw.Draw(Image.new('RGBA', (8, 8)))
    w = int(d.textlength(text, font=f)) + 56
    im = Image.new('RGBA', (w, 74), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, 73), 20, fill=(*fill, 238))
    d.text((28, 8), text, font=f, fill='white')
    return np.array(im)


def blend(frame, rgba, x, y, alpha=1.0):
    """Alpha-blend RGBA onto a BGR frame in place, clipped to bounds."""
    h, w = rgba.shape[:2]
    H_, W_ = frame.shape[:2]
    x, y = int(round(x)), int(round(y))
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W_, x + w), min(H_, y + h)
    if x1 <= x0 or y1 <= y0 or alpha <= .003:
        return
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    a = src[:, :, 3:4].astype(np.uint16)
    if alpha < .997:
        a = (a * int(alpha * 256)) >> 8
    if not a.any():
        return
    roi = frame[y0:y1, x0:x1]
    fg = src[:, :, 2::-1].astype(np.uint16)
    roi[:] = ((fg * a + roi.astype(np.uint16) * (255 - a) + 127) // 255).astype(np.uint8)


def scaled(rgba, s):
    if abs(s - 1) < .004:
        return rgba
    h, w = rgba.shape[:2]
    return cv2.resize(rgba, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_LINEAR)


# ---------------------------------------------------------------- thumbnails
def _pop(im):
    """Contrast + saturation lift so it reads at phone size."""
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.20, 0, 255)
    im = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)
    g = im.mean()
    if g < 74:
        im *= min(1.5, 92 / max(g, 1))
    im = np.clip((im - 128) * 1.10 + 130, 0, 255)
    return im.astype(np.uint8)


def thumbnail(frame, text, streamer, target, number=None, text_y=None, faces=None):
    """9:16 cover from a clean (overlay-free) frame. JPEG, well under API limits."""
    im = _pop(cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA) if frame.shape[:2] != (H, W) else frame)
    bl = cv2.GaussianBlur(im, (0, 0), 2.2)
    im = cv2.addWeighted(im, 1.35, bl, -.35, 0)
    pil = Image.fromarray(im[:, :, ::-1]).convert('RGBA')
    y0 = text_y if text_y is not None else 1180
    if faces:
        # Slide the headline to the clearest band so it never covers a face.
        best = None
        for y in (1180, 1250, 1120, 1040, 960, 900, 330):
            hit = sum(max(0, min(y + 250, (f[1] + f[3] * 1.05) * H) - max(y - 130, (f[1] - .05 * f[3]) * H)) * f[2] for f in faces)
            cost = hit + (90 if y == 330 else abs(y - y0) * .05)
            if best is None or cost < best[0]:
                best = (cost, y)
        y0 = best[1]
    grad = Image.new('L', (1, H), 0)
    for y in range(H):
        grad.putpixel((0, y), int((np.interp(y, [0, 230, y0 - 190, y0 + 60, y0 + 430, H], [110, 0, 0, 135, 100, 185]) if y0 > 600 else np.interp(y, [0, 420, 700, 1300, H], [175, 120, 0, 0, 170]))))
    pil = Image.composite(Image.new('RGBA', (W, H), (6, 9, 15, 255)), pil, grad.resize((W, H)))
    if text:
        hl = Image.fromarray(headline(text, 990, 3))
        pil.alpha_composite(hl, ((W - hl.width) // 2, int(y0 - hl.height / 2 + 60)))
    t = Image.fromarray(tag(streamer))
    pil.alpha_composite(t, ((W - t.width) // 2, 1500))
    if number:
        d = ImageDraw.Draw(pil)
        by = 60 if y0 > 600 else 1130          # stay clear of a headline that moved to the top
        d.ellipse((790, by, 1030, by + 240), fill=(*GOLD, 255), outline=(0, 0, 0, 255), width=10)
        f1, f2 = font(54, 'black'), font(120, 'black')
        d.text((910, by + 58), 'TOP', font=f1, fill=INK, anchor='mm')
        d.text((910, by + 148), str(number), font=f2, fill=INK, anchor='mm')
    pil.convert('RGB').save(target, 'JPEG', quality=92, optimize=True)
    return target


def cover(frame, face, text, streamer, target, number=None):
    """Shorts cover: one clear face, a few big words, nothing else."""
    h_, w_ = frame.shape[:2]
    x, y, w, h = face
    k = max(H * .25 / max(1, h * h_), H / h_, W / w_)
    k = min(k, max(H / h_, W / w_) * 2.6)
    cw, ch = W / k, H / k
    x0 = float(np.clip((x + w / 2) * w_ - cw / 2, 0, max(0, w_ - cw)))
    y0 = float(np.clip((y + h / 2) * h_ - ch * .34, 0, max(0, h_ - ch)))
    im = cv2.warpAffine(frame, np.array([[k, 0, -x0 * k], [0, k, -y0 * k]], np.float64), (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)
    im = _pop(im)
    im = cv2.addWeighted(im, 1.45, cv2.GaussianBlur(im, (0, 0), 2.4), -.45, 0)
    pil = Image.fromarray(im[:, :, ::-1]).convert('RGBA')
    g = np.clip(np.interp(np.arange(H), [0, 180, 900, 1180, 1560, H], [70, 0, 0, 150, 190, 235]), 0, 255).astype(np.uint8)
    pil = Image.composite(Image.new('RGBA', (W, H), (5, 8, 14, 255)), pil, Image.fromarray(np.repeat(g[:, None], W, 1)))
    d = ImageDraw.Draw(pil)
    words = (text or '').split()
    if words:
        best = None
        for n in range(1, min(3, len(words)) + 1):
            for cuts in _splits(len(words), n):
                rows = [' '.join(words[a:z]) for a, z in cuts]
                size = 186
                while size > 84 and max(d.textlength(r, font=font(size, 'black')) for r in rows) > W - 130:
                    size -= 6
                size = min(size, 440 // len(rows))
                v = size * (1 + .12 * len(rows)) - 30 * (len(rows) > 2)
                if best is None or v > best[0]:
                    best = (v, size, rows)
        _, size, rows = best
        f = font(size, 'black')
        lh = int(size * 1.04)
        yy = 1370 - lh * len(rows) / 2
        for i, r in enumerate(rows):
            d.text((W / 2, yy + i * lh + lh / 2), r, font=f, anchor='mm', fill=GOLD if i == len(rows) - 1 else (255, 255, 255), stroke_width=max(7, size // 14), stroke_fill=(0, 0, 0))
    t = Image.fromarray(tag(streamer))
    pil.alpha_composite(t, ((W - t.width) // 2, 1600))
    if number:
        d.ellipse((800, 80, 1020, 300), fill=(*GOLD, 255), outline=(0, 0, 0, 255), width=10)
        d.text((910, 142), 'TOP', font=font(50, 'black'), fill=INK, anchor='mm')
        d.text((910, 226), str(number), font=font(112, 'black'), fill=INK, anchor='mm')
    pil.convert('RGB').save(target, 'JPEG', quality=93, optimize=True)
    return target


def _splits(n, rows):
    """Ways to break n words into rows, in order."""
    if rows == 1:
        return [[(0, n)]]
    if rows == 2:
        return [[(0, a), (a, n)] for a in range(1, n)]
    return [[(0, a), (a, c), (c, n)] for a in range(1, n - 1) for c in range(a + 1, n)]
