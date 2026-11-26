"""Per-clip layout understanding: faces, webcam overlay, chat, captions, bars, shots.

All rectangles are normalised [x, y, w, h] in source-frame coordinates.

The webcam finder does not assume any corner. A composited overlay is found
because pixels on either side of its border belong to different videos, so they
do not move together over time; natural footage does not behave like that.
"""
from __future__ import annotations
import math, threading
from pathlib import Path
import cv2
import numpy as np

MODEL = Path(__file__).resolve().parent.parent / 'models' / 'face_detection_yunet_2023mar.onnx'
_yunet = threading.local()      # the OpenCV detector is not thread-safe


# ------------------------------------------------------------------ faces
def face_model_ready():
    return MODEL.is_file()


def _detector(w, h, thr):
    cache = _yunet.__dict__.setdefault('d', {})
    key = (w, h, thr)
    if key not in cache:
        cache[key] = cv2.FaceDetectorYN.create(str(MODEL), '', (w, h), thr, .3, 60)
    return cache[key]


def detect_faces(frames, thr=.6, upscale=1.0):
    """Per frame list of [x,y,w,h,score, 10 landmark coords], normalised."""
    out = []
    if not face_model_ready():
        return _haar_faces(frames)
    for im in frames:
        if upscale != 1:
            im = cv2.resize(im, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
        h, w = im.shape[:2]
        _, res = _detector(w, h, thr).detect(im)
        rows = []
        if res is not None:
            for r in res:
                bw, bh = r[2] / w, r[3] / h
                if bw < .012 or bh < .018:
                    continue
                lm = [float(v) / (w if i % 2 == 0 else h) for i, v in enumerate(r[4:14])]
                rows.append([float(r[0]) / w, float(r[1]) / h, float(bw), float(bh), float(r[14]), *lm])
        out.append(rows)
    return out


def _haar_faces(frames):
    det = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    out = []
    for im in frames:
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        h, w = g.shape
        rows = []
        for x, y, a, b in det.detectMultiScale(g, 1.1, 5, minSize=(22, 22)):
            rows.append([x / w, y / h, a / w, b / h, .65, *([0.0] * 10)])
        out.append(rows)
    return out


def cluster_faces(faces):
    """Group detections that stay in one place. Returns list of dicts by support."""
    cl = []
    for i, rows in enumerate(faces):
        used = set()
        for f in sorted(rows, key=lambda r: -r[4]):
            cx, cy, w = f[0] + f[2] / 2, f[1] + f[3] / 2, f[2]
            best, bd = None, 1e9
            for k, c in enumerate(cl):
                if k in used:
                    continue
                mx, my, mw = c['cx'], c['cy'], c['w']
                d = math.hypot((cx - mx), (cy - my) * 9 / 16)
                if d < 1.15 * mw and .5 < w / mw < 2.0 and d < bd:
                    best, bd = k, d
            if best is None:
                cl.append({'idx': [i], 'rows': [f], 'cx': cx, 'cy': cy, 'w': w})
                used.add(len(cl) - 1)
            else:
                c = cl[best]
                c['idx'].append(i)
                c['rows'].append(f)
                a = np.array(c['rows'])
                c['cx'] = float(np.median(a[:, 0] + a[:, 2] / 2))
                c['cy'] = float(np.median(a[:, 1] + a[:, 3] / 2))
                c['w'] = float(np.median(a[:, 2]))
                used.add(best)
    n = max(1, len(faces))
    for c in cl:
        a = np.array(c['rows'])
        c['support'] = len(set(c['idx'])) / n
        c['box'] = [float(v) for v in np.median(a[:, :4], axis=0)]
        c['spread'] = float(np.hypot(np.std(a[:, 0] + a[:, 2] / 2), np.std(a[:, 1] + a[:, 3] / 2) * 9 / 16) / max(c['w'], 1e-4))
        c['score'] = float(np.median(a[:, 4]))
    return sorted(cl, key=lambda c: -c['support'])


# --------------------------------------------------------- overlay boundaries
def overlay_maps(hi, lo):
    """(V, H, sd, cross): boundary strength of vertical / horizontal lines on the lo grid."""
    L = lo[::max(1, len(lo) // 220)].astype(np.float32)
    L = L - L.mean(0)
    sd = L.std(0)
    d = 2

    def dec(a, b, sa, sb):
        c = (a * b).mean(0) / (sa * sb + 1e-3)
        return np.clip(1 - c, 0, 1.2) * (np.maximum(sa, sb) > 4)
    h, w = sd.shape
    V = np.zeros((h, w), np.float32)
    H = np.zeros((h, w), np.float32)
    V[:, 1:-1] = dec(L[:, :, :-d], L[:, :, d:], sd[:, :-d], sd[:, d:])
    H[1:-1, :] = dec(L[:, :-d, :], L[:, d:, :], sd[:-d, :], sd[d:, :])
    G = hi[::max(1, len(hi) // 40)].astype(np.int16).max(axis=3)
    gx = np.median(np.abs(np.diff(G, axis=2, append=G[:, :, -1:])), axis=0).astype(np.float32)
    gy = np.median(np.abs(np.diff(G, axis=1, append=G[:, -1:, :])), axis=0).astype(np.float32)
    k = max(1, hi.shape[2] // w)

    def pool(a):
        a = a[:h * k, :w * k]
        a = np.pad(a, ((0, h * k - a.shape[0]), (0, w * k - a.shape[1])))
        return a.reshape(h, k, w, k).max(axis=(1, 3))
    gxl, gyl = pool(gx), pool(gy)
    raw = (np.minimum(V, H)).copy()
    # Decorrelation is the real evidence. A persistent edge alone (furniture,
    # posters, door frames) may support a border but can never prove one.
    BV = np.maximum(np.clip(V / .5, 0, 1), .52 * np.clip((gxl - 3) / 12, 0, 1))
    BH = np.maximum(np.clip(H / .5, 0, 1), .52 * np.clip((gyl - 3) / 12, 0, 1))

    def locked(a, axis):
        ks = (13, 1) if axis == 0 else (1, 13)
        kn = (5, 1) if axis == 0 else (1, 5)
        ref = (cv2.blur(a, ks) * 13 - cv2.blur(a, kn) * 5) / 8
        return pool(((a > 1.6 * ref + 2.2) & (a > 3.5)).astype(np.float32))
    return BV, BH, sd, raw, gx, gy, locked(gx, 0), locked(gy, 1)


def _refine(hi, box):
    """Snap a lo-resolution rectangle to exact edges using median hi-res gradients."""
    G = hi[::max(1, len(hi) // 60)].astype(np.int16).max(axis=3)
    H, W = G.shape[1:]
    x, y, w, h = box
    l, t, r, b = x * W, y * H, (x + w) * W, (y + h) * H

    def snap(axis, pos, a, c, lim):
        if pos < 3 or pos > lim - 3:
            return 0 if pos < 3 else lim
        lo_, hi_ = int(max(1, pos - 7)), int(min(lim - 1, pos + 7))
        a, c = int(a + 4), int(c - 4)
        if axis == 0:
            seg = np.abs(G[:, a:c, lo_:hi_ + 1] - G[:, a:c, lo_ - 1:hi_])
            prof = np.median(seg, axis=0).mean(axis=0) + .4 * seg.mean(axis=(0, 1))
        else:
            seg = np.abs(G[:, lo_:hi_ + 1, a:c] - G[:, lo_ - 1:hi_, a:c])
            prof = np.median(seg, axis=0).mean(axis=1) + .4 * seg.mean(axis=(0, 2))
        if prof.max() < 1.5 * np.median(prof) + .5:
            return pos
        return lo_ + int(np.argmax(prof))
    l2 = snap(0, l, t, b, W)
    r2 = snap(0, r, t, b, W)
    t2 = snap(1, t, l, r, H)
    b2 = snap(1, b, l, r, H)
    if r2 - l2 < .6 * (r - l) or b2 - t2 < .6 * (b - t):
        return box
    return [l2 / W, t2 / H, (r2 - l2) / W, (b2 - t2) / H]


def _crisp(gx, gy, box):
    """Fraction of each border that is a locked, razor-straight step.

    Composited borders sit on exactly the same pixel column for the whole clip.
    Returns one value per side, None for sides on the frame edge.
    """
    H, W = gx.shape
    x, y, w, h = box
    l, t, r, b = x * W, y * H, (x + w) * W, (y + h) * H

    def side(a, pos, s0, s1, lim):
        if pos < 4 or pos > lim - 5:
            return None
        s0, s1, p0 = int(s0 + .06 * (s1 - s0)), int(s1 - .06 * (s1 - s0)), int(round(pos))
        best = 0.0
        for c in range(p0 - 4, p0 + 5):
            if c < 2 or c > lim - 3:
                continue
            on = a[s0:s1, max(0, c - 1):c + 1].max(axis=1)
            near = np.concatenate([a[s0:s1, max(0, c - 10):max(0, c - 3)], a[s0:s1, c + 3:c + 10]], axis=1)
            if not near.shape[1]:
                continue
            ref = np.median(near, axis=1)
            best = max(best, float(((on > 1.7 * ref + 2.2) & (on > 3.5)).mean()))
        return best
    return [side(gx, l, t, b, W), side(gx, r, t, b, W), side(gy.T, t, l, r, H), side(gy.T, b, l, r, H)]


def find_webcam(hi, lo, faces, maps, prior=None):
    """Locate a composited webcam. Returns dict or None.

    kind 'box'     bordered or rectangular overlay (green screens that are visible
                   as a rectangle count as a box)
    kind 'cutout'  keyed / borderless streamer floating over the game
    """
    BV, BH, sd = maps[:3]
    h, w = sd.shape
    cl = [c for c in cluster_faces(faces) if c['support'] >= .22]
    if not cl:
        return None
    stable_big = [c for c in cl if c['support'] >= .5 and c['spread'] < 1.6]
    dilV = cv2.dilate(BV, np.ones((1, 3), np.uint8))
    dilH = cv2.dilate(BH, np.ones((3, 1), np.uint8))
    cumV = np.vstack([np.zeros((1, w), np.float32), np.cumsum(dilV, axis=0)])
    cumH = np.hstack([np.zeros((h, 1), np.float32), np.cumsum(dilH, axis=1)])

    def vline(x, t, b):
        if x <= 1 or x >= w - 2:
            return None
        return float((cumV[b, x] - cumV[t, x]) / max(1, b - t))

    def hline(y, l, r):
        if y <= 1 or y >= h - 2:
            return None
        return float((cumH[y, r] - cumH[y, l]) / max(1, r - l))
    cands = []
    for c in cl:
        fx, fy, fw, fh = c['box']
        if fw > .17 or fw < .014:
            continue
        # A larger face that is just as stable means this is ordinary footage.
        if any(o is not c and o['w'] > 1.7 * c['w'] and o['support'] >= .6 * c['support'] + .1 for o in stable_big):
            continue
        l0, r0 = int(fx * w), int(math.ceil((fx + fw) * w))
        t0, b0 = int(fy * h), int(math.ceil((fy + fh) * h))
        fW, fH = fw * w, fh * h
        rowsA, rowsB = max(0, int(t0 - .2 * fH)), min(h, int(b0 + .6 * fH))
        colsA, colsB = max(0, int(l0 - .4 * fW)), min(w, int(r0 + .4 * fW))

        def peaks(prof, lo_, hi_, edge):
            lo_, hi_ = max(0, int(lo_)), min(len(prof) - 1, int(hi_))
            if hi_ < lo_:
                return []
            order = [i for i in np.argsort(-prof[lo_:hi_ + 1]) + lo_ if prof[i] > .3]
            got = []
            for i in order:
                if all(abs(i - g) > 2 for g in got):
                    got.append(int(i))
                if len(got) >= 11:
                    break
            if edge is not None and abs((lo_ if edge == 0 else hi_) - edge) <= 1:
                got.append(edge)
            return sorted(set(got))
        pv = np.maximum(dilV[rowsA:rowsB].mean(axis=0), .98 * maps[6][rowsA:rowsB].mean(axis=0))
        ph = np.maximum(dilH[:, colsA:colsB].mean(axis=1), .98 * maps[7][:, colsA:colsB].mean(axis=1))
        Ls = peaks(pv, l0 - 9 * fW, l0 - .05 * fW, 0)
        Rs = peaks(pv, r0 + .05 * fW, r0 + 9 * fW, w - 1)
        Ts = peaks(ph, t0 - 5 * fH, t0 + .1 * fH, 0)
        Bs = peaks(ph, b0 + .02 * fH, b0 + 6 * fH, h - 1)
        cxp, cyp = (fx + fw / 2) * w, (fy + fh / 2) * h
        for l in Ls:
            for r in Rs:
                rw = r - l
                if not (1.5 * fW <= rw <= .46 * w):
                    continue
                for t in Ts:
                    for b in Bs:
                        rh = b - t
                        if not (1.25 * fH <= rh <= .56 * h):
                            continue
                        ar = (rw / w * 16) / (rh / h * 9)
                        if not .62 <= ar <= 2.5:
                            continue
                        if not (.10 <= (cxp - l) / rw <= .90 and .08 <= (cyp - t) / rh <= .80):
                            continue
                        sides = [vline(l, t, b), vline(r, t, b), hline(t, l, r), hline(b, l, r)]
                        real = [s for s in sides if s is not None]
                        if len(real) < 2 or min(real) < .45:
                            continue
                        mean = float(np.mean(real))
                        if mean < .62:
                            continue
                        snap = min(abs(ar - a) for a in (16 / 9, 4 / 3, 1.0))
                        area = rw * rh / (w * h)
                        s = mean + .03 * len(real) + .05 * (4 - len(real)) - .10 * min(1, snap) + .22 * c['support'] - .05 * abs((cxp - l) / rw - .5)
                        # Prefer the outermost strong rectangle over furniture edges inside it.
                        s += .9 * math.sqrt(area)
                        cands.append((s, [l / w, t / h, rw / w, rh / h], mean, min(real), c, len(real)))
    best = None
    if cands:
        cands.sort(key=lambda z: -z[0])
        ranked = []
        for s, box, mean, weakest, c, nreal in cands[:500]:
            cr = [v for v in _crisp(maps[4], maps[5], box) if v is not None]
            if len(cr) < 2:
                continue
            cm, cw = float(np.mean(cr)), float(min(cr))
            if cm < .66 or cw < .4:
                continue
            ranked.append((s + .9 * cm + .5 * cw, box, mean, weakest, c, cm, cw))
        if ranked:
            ranked.sort(key=lambda z: -z[0])
            _, box, mean, weakest, c, cm, cw = ranked[0]
            box = _refine(hi, box)
            conf = float(np.clip(.22 + .2 * (mean - .5) / .5 + .38 * cm + .12 * cw + .18 * c['support'], 0, .99))
            best = {'kind': 'box', 'rect': [round(v, 5) for v in box], 'face': c['box'], 'support': round(c['support'], 2),
                    'conf': round(conf, 2), 'edge': round(mean, 2), 'crisp': [round(cm, 2), round(cw, 2)]}
    if prior and (best is None or best['conf'] < .62):
        # Same streamer, same scene layout: accept the remembered box when a face
        # keeps appearing inside it and its edges are at least partly visible here.
        for pr in prior:
            x, y, pw, ph_ = pr['rect']
            inside = [c for c in cl if x <= c['cx'] <= x + pw and y <= c['cy'] <= y + ph_ and c['w'] < .8 * pw]
            if not inside:
                continue
            l, t, r, b = int(x * w), int(y * h), int((x + pw) * w), int((y + ph_) * h)
            real = [s for s in (vline(l, t, b), vline(min(r, w - 1), t, b), hline(t, l, min(r, w)), hline(min(b, h - 1), l, min(r, w))) if s is not None]
            if real and np.mean(real) > .42:
                c = inside[0]
                return {'kind': 'box', 'rect': list(pr['rect']), 'face': c['box'], 'support': round(c['support'], 2),
                        'conf': .72, 'edge': round(float(np.mean(real)), 2), 'from_prior': True}
    if best and best['conf'] >= .5:
        return best
    # Keyed streamer: one small face glued to the same place while everything else changes.
    moving = float(np.median(sd))
    for c in cl:
        fx, fy, fw, fh = c['box']
        others = [o for o in cl if o is not c and o['support'] > .35 and o['w'] > .7 * c['w']]
        edge_side = c['cx'] < .30 or c['cx'] > .70
        if c['support'] >= .55 and c['spread'] < .9 and fw < .095 and edge_side and not others and moving > 14:
            bw, bh = min(.42, 3.4 * fw), min(.6, 3.3 * fh)
            x = float(np.clip(c['cx'] - bw / 2, 0, 1 - bw))
            y = float(np.clip(fy - .75 * fh, 0, 1 - bh))
            # A base line under the streamer is usually visible.
            ph = dilH[:, int(max(0, (c['cx'] - fw) * w)):int(min(w, (c['cx'] + fw) * w))].mean(axis=1)
            lo_, hi_ = int((fy + 1.3 * fh) * h), int(min(h - 2, (fy + 4 * fh) * h))
            if hi_ > lo_:
                k = lo_ + int(np.argmax(ph[lo_:hi_]))
                if ph[k] > .6:
                    bh = max(1.9 * fh, k / h - y)
            return {'kind': 'cutout', 'rect': [round(x, 5), round(y, 5), round(bw, 5), round(min(bh, 1 - y), 5)], 'face': c['box'],
                    'support': round(c['support'], 2), 'conf': .55, 'edge': 0}
    return None


# ---------------------------------------------------------------- overlays
def find_chat(maps, webcam):
    """Dense, independently changing text blocks near the frame sides."""
    raw = maps[3]
    h, w = raw.shape
    m = cv2.blur((raw > .28).astype(np.float32), (9, 5))
    mask = (m > .42).astype(np.uint8)
    if webcam:
        x, y, a, b = webcam['rect']
        mask[int(max(0, y * h - 2)):int((y + b) * h + 2), int(max(0, x * w - 2)):int((x + a) * w + 2)] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 15), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, a, b, area = (int(v) for v in st[i])
        fa, fb = a / w, b / h
        if fa < .07 or fb < .035 or fa > .5 or fb > .6 or area / (a * b) < .5:
            continue
        cx = (x + a / 2) / w
        cy = (y + b / 2) / h
        if cy > .86 and .3 < cx < .7:          # caption band, handled separately
            continue
        if .34 < cx < .66 and .2 < cy < .8:    # centre action is not chat
            continue
        out.append([round(x / w, 4), round(y / h, 4), round(fa, 4), round(fb, 4)])
    out.sort(key=lambda r: -r[2] * r[3])
    return [r for r in out if r[2] * r[3] >= .34 * out[0][2] * out[0][3]][:2]


def find_captions(hi):
    """Twitch's baked-in auto captions: bold white words with a purple highlight."""
    n, H, W = hi.shape[:3]
    y0, y1, x0, x1 = int(.80 * H), int(.99 * H), int(.22 * W), int(.78 * W)
    hits, tops, bots = 0, [], []
    for f in hi[::max(1, n // 60)]:
        roi = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        pur = (roi[:, :, 0] >= 126) & (roi[:, :, 0] <= 146) & (roi[:, :, 1] > 105) & (roi[:, :, 2] > 120)
        wht = (roi[:, :, 1] < 40) & (roi[:, :, 2] > 220)
        rows = pur.sum(axis=1)
        band = rows > .012 * (x1 - x0)
        if band.sum() >= 4:
            ys = np.where(band)[0]
            if ys.max() - ys.min() < .10 * H and wht[ys.min():ys.max() + 1].mean() > .015:
                hits += 1
                tops.append(y0 + ys.min())
                bots.append(y0 + ys.max())
    total = len(hi[::max(1, n // 60)])
    if hits < max(2, .035 * total):
        return None
    return {'top': round(float(np.percentile(tops, 10)) / H - .012, 4), 'bottom': round(float(np.percentile(bots, 90)) / H + .02, 4),
            'share': round(hits / total, 2)}


def find_bars(hi):
    """Black letterbox / pillarbox margins that stay black for the whole clip."""
    f = hi[::max(1, len(hi) // 40)].max(axis=3)
    peak = np.percentile(f, 92, axis=0)
    dark = peak < 26
    H, W = dark.shape
    rows, cols = dark.mean(axis=1), dark.mean(axis=0)

    def run(a, thr):
        k = 0
        while k < len(a) // 3 and a[k] > thr:
            k += 1
        return k
    top, bot = run(rows, .52), run(rows[::-1], .52)
    left, right = run(cols, .86), run(cols[::-1], .86)
    out = [top / H, bot / H, left / W, right / W]
    return [round(v, 4) if v > .012 else 0.0 for v in out]


def shot_cuts(lo, rate):
    """Frame indices (lo rate) where a new shot starts."""
    cuts = [0]
    prev = None
    ph = None
    for i, g in enumerate(lo):
        s = cv2.resize(g, (48, 27), interpolation=cv2.INTER_AREA).astype(np.float32)
        hist = cv2.calcHist([g], [0], None, [32], [0, 256])
        cv2.normalize(hist, hist, 1, 0, cv2.NORM_L1)
        if prev is not None:
            d = float(np.abs(s - prev).mean()) / 255
            hd = cv2.compareHist(ph, hist, cv2.HISTCMP_BHATTACHARYYA)
            if ((d > .13 and hd > .38) or d > .27) and i - cuts[-1] >= rate * .4:
                cuts.append(i)
        prev, ph = s, hist
    return cuts


def _mask(shape, rects, pad=0):
    h, w = shape
    m = np.zeros((h, w), bool)
    for x, y, a, b in rects:
        m[max(0, int(y * h) - pad):int((y + b) * h) + pad, max(0, int(x * w) - pad):int((x + a) * w) + pad] = True
    return m


def curves(lo, rate, webcam, chat, caps):
    """Motion curves sampled at the lo rate, each 0..1-ish, plus game interest track."""
    n, h, w = lo.shape
    cam_m = _mask((h, w), [webcam['rect']]) if webcam else np.zeros((h, w), bool)
    chat_m = _mask((h, w), chat) if chat else np.zeros((h, w), bool)
    skip = cam_m | chat_m
    if caps:
        skip[int(caps['top'] * h):, :] = True
    game_m = ~skip
    glob, cam, ch, gx = [0.0], [0.0], [0.0], []
    prof0 = np.zeros(w, np.float32)
    prev = lo[0].astype(np.int16)
    gcount = np.maximum(game_m.sum(axis=0), 1)
    gx.append(prof0)
    for i in range(1, n):
        cur = lo[i].astype(np.int16)
        d = np.abs(cur - prev)
        prev = cur
        glob.append(float(d[game_m].mean()) if game_m.any() else 0)
        cam.append(float(d[cam_m].mean()) if cam_m.any() else 0)
        ch.append(float((d[chat_m] > 30).mean()) if chat_m.any() else 0)
        gx.append((np.where(game_m, np.minimum(d, 60), 0).sum(axis=0) / gcount).astype(np.float32))
    return np.array(glob), np.array(cam), np.array(ch), np.stack(gx)


def interest_track(gx, rate, hi_faces, hi_rate, webcam, n_out):
    """Where the action is horizontally (0..1) at each lo sample, before smoothing."""
    n, w = gx.shape
    xs = (np.arange(w) + .5) / w
    prior = np.exp(-.5 * ((xs - .5) / .20) ** 2)
    k = max(3, int(rate * 1.2) | 1)
    sm = cv2.blur(gx, (1, k), borderType=cv2.BORDER_REPLICATE)
    sm = cv2.blur(sm, (max(3, w // 24) | 1, 1))
    out = np.full(n, .5)
    strength = np.zeros(n)
    for i in range(n):
        p = sm[i]
        # Global camera movement lights up everything evenly; only local excess counts.
        p = np.clip(p - np.median(p), 0, None) * prior
        s = p.sum()
        if s > 1.5:
            out[i] = float((p * xs).sum() / s)
            strength[i] = min(1.0, s / 60)
        j = min(len(hi_faces) - 1, int(i / rate * hi_rate))
        best = None
        for f in hi_faces[j] if hi_faces else []:
            cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
            if webcam:
                x, y, a, b = webcam['rect']
                if x - .01 <= cx <= x + a + .01 and y - .01 <= cy <= y + b + .01:
                    continue
            if f[4] > .7 and f[2] > .035 and (best is None or f[2] > best[2]):
                best = f
        if best is not None:
            out[i] = .75 * (best[0] + best[2] / 2) + .25 * out[i]
            strength[i] = max(strength[i], .8)
    return out, strength


def green_screen(hi, rect):
    """Visible (unkeyed) green backdrop inside the webcam box."""
    H, W = hi.shape[1:3]
    x, y, a, b = rect
    r, cols = [], []
    for f in hi[::max(1, len(hi) // 12)]:
        roi = f[int(y * H):int((y + b) * H), int(x * W):int((x + a) * W)]
        if not roi.size:
            continue
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        m = (hsv[:, :, 0] >= 38) & (hsv[:, :, 0] <= 86) & (hsv[:, :, 1] >= 110) & (hsv[:, :, 2] >= 70)
        r.append(float(m.mean()))
        if m.sum() > 50:
            cols.append(np.median(roi[m], axis=0))
    if not r or np.median(r) < .25 or not cols:
        return None
    c = np.median(cols, axis=0)
    return {'bgr': [int(v) for v in c], 'share': round(float(np.median(r)), 2)}


# ------------------------------------------------------------- subject tracks
def _look(im, f):
    """Clothing/hair colour signature so people keep their identity while the camera swings."""
    H, W = im.shape[:2]
    x, y, w, h = f[0] * W, f[1] * H, f[2] * W, f[3] * H
    out = []
    for x0, x1, y0, y1 in ((x - .35 * w, x + 1.35 * w, y + 1.15 * h, y + 2.5 * h), (x - .1 * w, x + 1.1 * w, y - .35 * h, y + .15 * h)):
        roi = im[int(max(0, y0)):int(min(H, y1)), int(max(0, x0)):int(min(W, x1))]
        if roi.size < 120:
            out.append(np.zeros(60, np.float32))
            continue
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1, 2], None, [5, 3, 4], [0, 180, 0, 256, 0, 256]).ravel()
        out.append(hist / (hist.sum() + 1e-6))
    return np.sqrt(np.concatenate(out))


def _unlike(a, b):
    """0 identical .. 1 unrelated (Hellinger on the torso part, lighter on hair)."""
    t = 1 - float(a[:60] @ b[:60])
    r = 1 - float(a[60:] @ b[60:])
    return .75 * t + .25 * r


def subject_tracks(faces, rate, cuts_s, dur, frames=None):
    """Link faces into per-shot people for real-world footage."""
    tracks = []
    bounds = list(cuts_s) + [dur + 1]
    for a, b in zip(bounds, bounds[1:]):
        live = []
        for i in range(len(faces)):
            t = (i + .5) / rate
            if not a <= t < b:
                continue
            rows = [f for f in faces[i] if f[4] > .6 and f[2] > .02]
            pairs = []
            looks = [(_look(frames[i], f) if frames is not None else None) for f in rows]
            for r, f in enumerate(rows):
                cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
                for k, tr in enumerate(live):
                    gap = t - tr['t'][-1]
                    if gap <= 0 or gap > max(2.2, 3 / rate):
                        continue
                    lw = tr['w'][-1]
                    d = math.hypot(cx - tr['cx'][-1], (cy - tr['cy'][-1]) * 9 / 16) / max(2.2 * lw, .085)
                    if d > 1.5 + .5 * min(1.5, gap) or not .42 < f[2] / lw < 2.4:
                        continue
                    u = _unlike(looks[r], tr['look']) if looks[r] is not None else .3
                    if u > .62 and d > .45:
                        continue
                    pairs.append((d + 1.5 * u + .15 * gap, r, k))
            ur, uk = set(), set()
            for c, r, k in sorted(pairs):
                if r in ur or k in uk or c > 2.4:
                    continue
                ur.add(r)
                uk.add(k)
                _extend(live[k], t, rows[r], looks[r])
            for r, f in enumerate(rows):
                if r not in ur:
                    tr = {'t': [], 'cx': [], 'cy': [], 'w': [], 'h': [], 'score': [], 'shot': (a, min(b, dur)), 'look': looks[r]}
                    live.append(tr)
                    _extend(tr, t, f, looks[r])
        # Re-join the same person after they left frame or turned away.
        live.sort(key=lambda q: q['t'][0])
        merged = []
        for tr in live:
            best, bu = None, .24
            for m in merged:
                if m['t'][-1] < tr['t'][0] and tr['look'] is not None:
                    u = _unlike(m['look'], tr['look'])
                    if u < bu:
                        best, bu = m, u
            if best is not None:
                for key in ('t', 'cx', 'cy', 'w', 'h', 'score'):
                    best[key] += tr[key]
                best['look'] = .5 * best['look'] + .5 * tr['look']
            else:
                merged.append(tr)
        tracks += merged
    for tr in tracks:
        tr.pop('look', None)
    return tracks


def _extend(tr, t, f, look):
    tr['t'].append(t)
    tr['cx'].append(f[0] + f[2] / 2)
    tr['cy'].append(f[1] + f[3] / 2)
    tr['w'].append(f[2])
    tr['h'].append(f[3])
    tr['score'].append(f[4])
    if look is not None and tr['look'] is not None:
        tr['look'] = .8 * tr['look'] + .2 * look
