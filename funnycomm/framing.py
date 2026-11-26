"""Steady framing: decide the few fixed shots a Short is made of.

A tripod operator, not a tracking one. Inside a shot the crop never moves. The
planner picks, for every stretch of the clip, the tightest fixed shot that keeps
everybody who matters and whatever is happening in view. When one portrait crop
would cut people or the action off, it opens to a bigger shot instead of chasing.
"""
from __future__ import annotations
import cv2
import numpy as np
from . import media
from .audio import smooth

COLS, RATE, PW, PH = 48, 8.0, 192, 108
STEP = .25                                  # planning grid, seconds
PANELS = (1920, 1350, 1080, 860, 700)       # picture height on a 1080-wide Short
MIN_SHOT = 2.0
DUO = 1220                                  # close-up height under a wide shot
SQUARE = 1080                               # one square picture of the whole scene
PIP = 852                                   # webcam inset over a square picture: 380 x 300
FIT = .08                                   # how much worse a full-screen crop may be before the square wins


# ------------------------------------------------------------- what is moving
def profile(path, meta, rects=()):
    """Where things move, with the camera's own movement taken out."""
    raw = media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', path, '-an', '-vf', f'fps={RATE},scale={PW}:{PH}:flags=area,format=gray',
                     '-f', 'rawvideo', 'pipe:1']).stdout
    f = np.frombuffer(raw, np.uint8)
    f = f[:len(f) // (PW * PH) * (PW * PH)].reshape(-1, PH, PW)
    ok = np.ones((PH, PW), np.float32)
    for x, y, w, h in rects:
        ok[max(0, int(y * PH) - 1):int((y + h) * PH) + 2, max(0, int(x * PW) - 1):int((x + w) * PW) + 2] = 0
    cols = np.zeros((len(f), COLS), np.float32)
    rows = np.zeros((len(f), 27), np.float32)
    shake = np.zeros(len(f), np.float32)
    win = cv2.createHanningWindow((PW, PH), cv2.CV_32F)
    full = np.maximum(ok.sum(axis=0), 1)
    for i in range(1, len(f)):
        a, b = f[i - 1].astype(np.float32), f[i].astype(np.float32)
        (dx, dy), resp = cv2.phaseCorrelate(a * ok, b * ok, win)
        lost = resp < .04 or abs(dx) > PW * .3 or abs(dy) > PH * .3
        if lost:
            dx = dy = 0.0
        shake[i] = -1 if lost else float(np.hypot(dx, dy)) / PW * RATE
        moved = cv2.warpAffine(a, np.array([[1, 0, dx], [0, 1, dy]], np.float64), (PW, PH), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        sb = cv2.GaussianBlur(b, (3, 3), 0)
        d = np.clip(np.abs(sb - cv2.GaussianBlur(moved, (3, 3), 0)) - 5, 0, 55) * ok
        if dx or dy:                                  # a video playing on a still page is not camera movement
            d0 = np.clip(np.abs(sb - cv2.GaussianBlur(a, (3, 3), 0)) - 5, 0, 55) * ok
            if d0.mean() <= d.mean() * 1.02:
                d = d0
                shake[i] = 0
        cols[i] = cv2.resize((d.sum(axis=0) / full)[None], (COLS, 1), interpolation=cv2.INTER_AREA)[0]
        rows[i] = cv2.resize(d.mean(axis=1)[:, None], (1, 27), interpolation=cv2.INTER_AREA)[:, 0]
    for i in range(1, len(shake)):
        if shake[i] < 0:
            shake[i] = shake[i - 1]
    return {'rate': RATE, 'cols': [[round(float(v), 1) for v in r] for r in cols], 'rows': [[round(float(v), 1) for v in r] for r in rows],
            'shake': [round(float(v), 2) for v in shake]}


def ensure(an, path):
    """Attach the movement profile to an analysis. Returns True when it had to be measured."""
    if an.get('move') and an['move'].get('rate') == RATE:
        return False
    L = an['layout']
    rects = list(L.get('chat') or []) + ([L['cam']['rect']] if L.get('cam') else [])
    if L.get('captions'):
        rects.append([0, L['captions']['top'], 1, 1 - L['captions']['top']])
    try:
        an['move'] = profile(path, an['meta'], rects)
    except media.MediaError:
        an['move'] = {'rate': RATE, 'cols': [], 'rows': [], 'shake': []}
    return True


def _moving(an, ts):
    """(T, COLS) energy shares, strength 0..1, camera shake, all on the planning grid."""
    mv = an.get('move') or {}
    cols = np.array(mv.get('cols') or np.zeros((2, COLS)), np.float32)
    shake = np.array(mv.get('shake') or [0, 0], np.float32)
    rate = mv.get('rate', RATE)
    k = max(1, int(rate * 1.1) | 1)
    sm = cv2.blur(cols, (1, k), borderType=cv2.BORDER_REPLICATE)
    sm = cv2.blur(sm, (3, 1), borderType=cv2.BORDER_REPLICATE)
    sm = np.clip(sm - np.median(sm, axis=1, keepdims=True), 0, None)        # only local movement counts
    e = sm[np.clip((ts * rate).astype(int), 0, len(sm) - 1)]
    tot = e.sum(axis=1)
    sh = cv2.blur(shake[:, None], (1, k), borderType=cv2.BORDER_REPLICATE)[np.clip((ts * rate).astype(int), 0, len(shake) - 1), 0]
    hi = max(24.0, float(np.percentile(tot, 85)) if len(tot) else 24.0)
    strength = np.clip((tot - 6) / hi, 0, 1) * np.clip(1.35 - sh / 1.1, .25, 1)
    share = e / np.maximum(tot[:, None], 1e-3)
    return share, strength, sh


def unsteady(an):
    """Share of the clip where the original camera itself is swinging too fast to follow."""
    sh = np.array((an.get('move') or {}).get('shake') or [0], np.float32)
    s = cv2.blur(sh[:, None], (1, 5), borderType=cv2.BORDER_REPLICATE)[:, 0]
    return float((s > .85).mean())


def still(an):
    """True when there is no webcam and the picture hardly moves: a photo or a paused page, not a moment."""
    mv = an.get('move') or {}
    if an['layout'].get('cam') or len(mv.get('cols') or []) < 8:
        return False
    tot = np.array(mv['cols'], np.float32).sum(axis=1)
    return bool((tot < 12).mean() > .85 and np.median(mv['shake']) < .02)


def wildness(an):
    """Score points to take off because the original camera work is hard to follow."""
    sh = np.array((an.get('move') or {}).get('shake') or [0], np.float32)
    b = cv2.blur(sh[:, None], (1, 5), borderType=cv2.BORDER_REPLICATE)[:, 0]
    return float(30 * np.clip((b > .35).mean() - .20, 0, .3) + 60 * np.clip((b > .85).mean() - .04, 0, .2))


# ---------------------------------------------------------------- candidates
def _box(an):
    """Usable picture area in source pixels."""
    from . import camera
    x0, y0, x1, y1 = camera.usable(an)
    sw, sh = an['meta']['w'], an['meta']['h']
    return x0 * sw, y0 * sh, x1 * sw, y1 * sh


def _crops(box, heights, step=.045):
    """Every fixed crop on offer: (panel height, x0, x1, y0, y1)."""
    x0, y0, x1, y1 = box
    uw, uh = x1 - x0, y1 - y0
    out = []
    for ph in heights:
        ch, cw = uh, uh * 1080 / ph
        if cw > uw:
            cw, ch = uw, uw * ph / 1080
            if ph != heights[-1] and ch < uh * .80:
                continue
        n = max(1, int(round((uw - cw) / max(1, uw * step))) + 1)
        for k in range(n):
            a = x0 + (uw - cw) * (k / (n - 1) if n > 1 else .5)
            out.append((ph, a, a + cw, y0 + (uh - ch) / 2, y0 + (uh + ch) / 2))
    return out


def _outside(lo, hi, a, b):
    """Fraction of [lo,hi] (arrays over time) outside the crop [a,b]."""
    return np.clip((np.maximum(0, a - lo) + np.maximum(0, hi - b)) / np.maximum(hi - lo, 1e-6), 0, 1)


def _people(an, ts, shot, sw):
    """Faces that matter in one shot, sampled on ts."""
    a, b = shot
    length = max(.2, b - a)
    tr = []
    for t in an['tracks']:
        if len(t['t']) < 2 or (abs(t['shot'][0] - a) > .06 and not a <= t['t'][0] < b):
            continue
        tt = np.array(t['t'])
        cov = float(min(1, np.minimum(np.diff(tt, append=tt[-1] + .3), .9).sum() / length))
        tr.append({'t': t, 'w': float(np.median(t['w'])), 'cov': cov})
    big = max([q['w'] * (.4 + .6 * q['cov']) for q in tr], default=0)
    talk = np.zeros(len(ts))
    for w, s, e, p in an['speech']['words']:
        talk[(ts >= s - .1) & (ts <= e + .15)] = 1
    out = []
    for q in tr:
        rel = q['w'] * (.4 + .6 * q['cov']) / max(big, 1e-6)
        if q['w'] < .02 or rel < .34:
            continue
        t = q['t']
        tt = np.array(t['t'])
        idx = np.clip(np.searchsorted(tt, ts), 0, len(tt) - 1)
        near = np.minimum(np.abs(tt[idx] - ts), np.abs(tt[np.maximum(idx - 1, 0)] - ts))
        live = np.clip(1.6 - near / 1.1, 0, 1)                       # fades out a second or two after the face was last seen
        cx = np.interp(ts, tt, smooth(np.array(t['cx']), 3)) * sw
        cy = np.interp(ts, tt, smooth(np.array(t['cy']), 3))
        w = np.interp(ts, tt, smooth(np.array(t['w']), 5)) * sw
        act = np.clip(np.interp(ts, t['act_t'], t['act'], left=0, right=0), 0, 6) / 6 if t.get('act') else np.zeros(len(ts))
        wt = (.35 + .65 * min(1, rel)) * (1 + .5 * act * talk) * live
        out.append({'cx': cx, 'cy': cy, 'w': w, 'lo': cx - 1.2 * w, 'hi': cx + 1.2 * w, 'wt': wt, 'live': live, 'rel': rel, 'cov': q['cov']})
    out.sort(key=lambda p: -p['rel'])
    return out[:5]


# --------------------------------------------------------------- the planner
def _solve(cost, ph, real, move=2.0, reshape=.7, at_cut=.45):
    """Cheapest sequence of fixed shots. cost (T,S); ph panel id per state; real: grid indices of real cuts."""
    T, S = cost.shape
    ph = np.asarray(ph)
    same = ph[:, None] == ph[None, :]
    inside = np.where(same, move, move + reshape)
    inside[np.arange(S), np.arange(S)] = 0
    cut = np.where(same, 0.0, at_cut)
    acc = cost[0].copy()
    back = np.zeros((T, S), np.int32)
    for t in range(1, T):
        m = acc[:, None] + (cut if t in real else inside)
        back[t] = np.argmin(m, axis=0)
        acc = m[back[t], np.arange(S)] + cost[t]
    path = np.zeros(T, int)
    path[-1] = int(np.argmin(acc))
    for t in range(T - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    return path


def _runs(path, real):
    out, a = [], 0
    for t in range(1, len(path) + 1):
        if t == len(path) or path[t] != path[a]:
            out.append([a, t, int(path[a])])
            a = t
    return out


def _tidy(runs, cost, ids, real):
    """Join shots too short to read to whichever neighbour costs least.

    A shot between two real cuts of the original keeps its own position but may
    not bring its own shape: the picture area stays put through quick cutting.
    """
    need = int(round(MIN_SHOT / STEP))
    runs = [list(r) for r in runs]
    for _ in range(60):
        short = sorted((r[1] - r[0], k) for k, r in enumerate(runs) if r[1] - r[0] < need and not r[3:])
        if not short or len(runs) < 2:
            break
        k = short[0][1]
        a, b, _s = runs[k][:3]
        opts = []
        for j in (k - 1, k + 1):
            if not 0 <= j < len(runs):
                continue
            free = a in real if j < k else (b in real)
            pool = [z for z in range(cost.shape[1]) if ids[z] == ids[runs[j][2]]] if free else [runs[j][2]]
            z = min(pool, key=lambda v: cost[a:b, v].mean())
            opts.append((float(cost[a:b, z].mean()), j, z))
        _, j, z = min(opts)
        if z == runs[j][2]:
            lo, hi = sorted((j, k))
            runs[lo:hi + 1] = [[runs[lo][0], runs[hi][1], z]]
        else:
            runs[k] = [a, b, z, True]            # a genuinely short source shot
    out = [runs[0][:3]]
    for r in runs[1:]:
        if r[2] == out[-1][2]:
            out[-1][1] = r[1]
        else:
            out.append(r[:3])
    return out


def _grid(an, dur, keep):
    """Kept seconds only, so nothing that was cut away influences the framing."""
    spans = keep or [[0, dur]]
    ts, real = [], {0}
    for a, b in spans:
        real.add(len(ts))
        n = max(1, int(np.ceil((b - a) / STEP)))
        ts += [a + (k + .5) * (b - a) / n for k in range(n)]
    ts = np.array(ts)
    for c in an['layout']['cuts']:
        k = int(np.searchsorted(ts, c))
        if c > 0 and 0 < k < len(ts):
            real.add(k)
    return ts, real


def _segments(ts, runs, make, an):
    """Runs on the planning grid -> shots in source seconds; changes at a real cut land exactly on it."""
    segs = []
    for n, (a, b, s) in enumerate(runs):
        t0 = float(ts[a] - STEP) if n == 0 else float(ts[a - 1] + ts[a]) / 2
        t1 = float(ts[b - 1] + STEP) if n == len(runs) - 1 else float(ts[b - 1] + ts[b]) / 2
        segs.append({'a': t0, 'b': t1, **make(s, a, b)})
    cuts = [c for c in an['layout']['cuts'] if c > 0]
    for p, q in zip(segs, segs[1:]):
        near = [c for c in cuts if abs(c - q['a']) < STEP]
        if near:
            p['b'] = q['a'] = float(min(near, key=lambda v: abs(v - q['a'])))
    return segs


def _shots(an, ts, dur):
    cuts = sorted(an['layout']['cuts']) + [dur + 1]
    if cuts[0] > 0:
        cuts = [0.0] + cuts
    shots = list(zip(cuts, cuts[1:]))
    which = np.clip(np.searchsorted(np.array([a for a, _ in shots]), ts, side='right') - 1, 0, len(shots) - 1)
    return shots, which


BASE = {1920: 0.0, 1350: .06, 1080: .09, 860: .16, 700: .27}        # price of a smaller picture
SWING = {1920: .20, 1350: .10, 1080: .04, 860: 0, 700: 0}            # price of a tight crop while the original camera swings


def plan_irl(an, dur, keep=None, people=True, heights=PANELS, force=False, fill=True, stack=None):
    sw, sh = an['meta']['w'], an['meta']['h']
    box = _box(an)
    ts, real = _grid(an, dur, keep)
    T = len(ts)
    crops = _crops(box, heights)
    share, strength, shake = _moving(an, ts)
    edges = np.linspace(0, sw, COLS + 1)
    cum = np.concatenate([np.zeros((T, 1)), np.cumsum(share, axis=1)], axis=1)
    shots, which = _shots(an, ts, dur)
    ppl = {n: (_people(an, ts, (shots[n][0], min(shots[n][1], dur)), sw) if people else []) for n in sorted(set(which.tolist()))}
    chat = [(x * sw, y * sh, (x + w) * sw, (y + h) * sh) for x, y, w, h in an['layout'].get('chat') or []]
    seen = np.zeros(T)
    for n, ps in ppl.items():
        for p in ps:
            seen = np.maximum(seen, np.where(which == n, p['wt'] * (p['live'] > .3), 0))
    def price(ph, a, b, y0, y1):
        cw = b - a
        c = np.full(T, BASE.get(ph, 0.0))
        for n, ps in ppl.items():
            sel = which == n
            for j, p in enumerate(ps):
                out = _outside(p['lo'], p['hi'], a, b)
                mid = ((p['cx'] < a + .30 * p['w']) | (p['cx'] > b - .30 * p['w'])).astype(float)
                c[sel] += (p['wt'] * (.55 * out + .60 * mid))[sel]
                if j == 0:
                    off = np.abs(p['cx'] - (a + b) / 2) / cw                    # nose against the edge of the frame
                    c[sel] += (p['wt'] * .10 * np.clip(off - .22, 0, .3) / .3)[sel]
                    on = p['w'] / cw                                             # too small to read, or too close to make sense of
                    c[sel] += (p['live'] * (.55 * np.clip((.085 - on) / .085, 0, 1) + .28 * np.clip((on - .50) / .30, 0, 1)))[sel]
        ia, ib = np.interp([a, b], edges, np.arange(COLS + 1))
        inside = cum[:, min(COLS, int(np.ceil(ib)))] - cum[:, int(ia)]
        c += np.where(seen > .4, .55, .80) * strength * np.clip((1 - inside) - .14, 0, 1) / .86
        c += np.clip(shake - .25, 0, .8) * SWING.get(ph, .10)
        hit = sum(max(0, min(b, q[2]) - max(a, q[0])) * max(0, min(y1, q[3]) - max(y0, q[1])) for q in chat) / (cw * (y1 - y0))
        c += min(.16, .9 * hit)
        c += np.where((seen < .2) & (strength < .2), .12 * abs((a + b) / 2 - sw / 2) / sw, 0)
        return c
    cost = np.stack([price(*c) for c in crops], axis=1)
    states = [('one', c) for c in crops]
    ids = [c[0] for c in crops]
    extra = []
    for n, ps in (ppl.items() if (heights is PANELS if stack is None else stack) else ()):        # two people far apart: stacked panels, fixed for the whole shot
        sel = np.flatnonzero(which == n)
        if len(ps) < 2 or len(sel) * STEP < 4.5:
            continue
        p, q = ps[0], ps[1]
        if min(p['cov'], q['cov']) < .42 or q['rel'] < .42:
            continue
        pair = _pair(p, q, sel, box, sw, sh)
        if pair is None:
            continue
        col = np.full(T, 9.0)
        col[sel] = pair[2]
        extra.append(col)
        states.append(('stack', pair[:2]))
        ids.append(9000)
    if extra:
        cost = np.concatenate([cost, np.stack(extra, axis=1)], axis=1)
    path = _solve(cost * STEP, ids, real)
    runs = _tidy(_runs(path, real), cost, ids, real)

    if fill:
        runs = _fill(runs, states, _crops(box, (DUO,)), _crops(box, (1920 - DUO,)), price, real, seen, force)
    mid = lambda q: ((q[1] + q[2]) / 2, (q[3] + q[4]) / 2, q[4] - q[3])

    def make(s, a, b):
        if isinstance(s, tuple):
            return {'kind': 'duo', 'top': 1920 - DUO, 'wide': mid(s[0]), 'crop': mid(s[1])}
        kind, v = states[s]
        if kind == 'stack':
            return {'kind': 'stack', 'A': v[0], 'B': v[1]}
        return {'kind': 'one', 'ph': v[0], 'crop': mid(v)}
    return {'mode': 'steady', 'segs': _segments(ts, runs, make, an), 'box': box, 'cost': float(cost[np.arange(T), path].mean())}


def _fill(runs, states, close, wide, price, real, seen, force=False):
    """Very wide shots fill the phone: everything on top, a steady close-up of whoever matters below."""
    big = [r for r in runs if states[r[2]][0] == 'one' and states[r[2]][1][0] < 1920]
    if not big:
        return runs
    at = np.concatenate([np.arange(r[0], r[1]) for r in big])
    pc = np.stack([price(*c) for c in close], axis=1)
    path = _solve(pc * STEP, [DUO] * len(close), real, move=2.4)
    low = _tidy(_runs(path, real), pc, [DUO] * len(close), real)
    line = np.zeros(len(path), int)
    for a, b, s in low:
        line[a:b] = s
    if not force and seen[at].mean() < .06:
        return runs                                        # nobody to show close up
    out = []
    for r in runs:
        if r not in big:
            out.append(r)
            continue
        a, b, _ = r
        w = min(wide, key=lambda c: price(*c)[a:b].mean())
        parts = [[max(a, x), min(b, y), z] for x, y, z in low if min(b, y) > max(a, x)]
        while len(parts) > 1 and min(y - x for x, y, _ in parts) * STEP < 1.5:
            k = min(range(len(parts)), key=lambda i: parts[i][1] - parts[i][0])
            j = k - 1 if k else 1
            lo, hi = sorted((j, k))
            parts[lo:hi + 1] = [[parts[lo][0], parts[hi][1], parts[j][2]]]
        out += [[x, y, (w, close[z])] for x, y, z in parts]
    return out


def _pair(p, q, sel, box, sw, sh):
    """Two fixed close-ups, or None when they would fit one normal shot or move too much."""
    x0, y0, x1, y1 = box
    gap = float(np.median(np.abs(p['cx'][sel] - q['cx'][sel])))
    if gap < .22 * sw:
        return None
    w = max(float(np.median(p['w'][sel])), float(np.median(q['w'][sel])))
    if w < .05 * sw:                                # strangers in the distance are not a conversation
        return None
    span = max(float(np.ptp(np.percentile(r['cx'][sel], [10, 90]))) for r in (p, q))
    pw = float(np.clip(max(3.4 * w, span + 2.2 * w), .30 * sw, min(.56 * sw, gap * 1.15)))
    ph = pw * 960 / 1080
    if ph > y1 - y0:
        ph = y1 - y0
        pw = ph * 1080 / 960
    out, res = np.full(len(sel), .10), []
    for r in (p, q):
        cx = float(np.clip(np.median(r['cx'][sel]), x0 + pw / 2, x1 - pw / 2))
        cy = float(np.clip(np.median(r['cy'][sel]) * sh + .10 * ph, y0 + ph / 2, y1 - ph / 2))
        miss = ((r['cx'][sel] < cx - pw / 2 + .5 * r['w'][sel]) | (r['cx'][sel] > cx + pw / 2 - .5 * r['w'][sel])).astype(float)
        out += r['live'][sel] * miss * .6 + (1 - r['live'][sel]) * .34
        res.append((cx, cy, ph))
    if float(out.mean()) > .34:
        return None
    res.sort(key=lambda v: v[0])
    return res[0], res[1], out


# -------------------------------------------------------------- webcam layouts
def _cam_shot(an, height):
    """One fixed webcam crop that holds the face for (nearly) the whole clip."""
    sw, sh = an['meta']['w'], an['meta']['h']
    cam = an['layout']['cam']
    x, y, w, h = cam['rect']
    ins = 2 / sw if cam['kind'] == 'box' else 0
    X0, Y0, X1, Y1 = (x + ins) * sw, (y + ins) * sh, (x + w - ins) * sw, (y + h - ins) * sh
    asp = 1080 / height
    full = min(Y1 - Y0, (X1 - X0) / asp)
    ch = full
    cx, cy = (X0 + X1) / 2, Y0 + .46 * (Y1 - Y0)
    fc = an['face_cam']
    if fc:
        F = np.array([[r[1] * sw, r[2] * sh, r[3] * sw, r[4] * sh] for r in fc])
        L, R, T, B = F[:, 0] - .45 * F[:, 2], F[:, 0] + 1.45 * F[:, 2], F[:, 1] - .62 * F[:, 3], F[:, 1] + 1.55 * F[:, 3]
        zooms = (1.0, 1.1, 1.22) if cam.get('people', 1) == 1 and cam['kind'] == 'box' else (1.0,)
        best = None
        for z in zooms:
            h_ = full / z
            w_ = h_ * asp
            for px in np.linspace(X0 + w_ / 2, max(X0 + w_ / 2, X1 - w_ / 2), 9):
                for py in np.linspace(Y0 + h_ / 2, max(Y0 + h_ / 2, Y1 - h_ / 2), 9):
                    out = np.clip((np.maximum(0, px - w_ / 2 - L) + np.maximum(0, R - px - w_ / 2)) / (R - L), 0, 1)
                    out = out + np.clip((np.maximum(0, py - h_ / 2 - T) + .5 * np.maximum(0, B - py - h_ / 2)) / (B - T), 0, 1)
                    v = float(out.mean()) + .10 * float(np.mean(np.abs((L + R) / 2 - px)) / w_) - .06 * (z - 1)
                    if best is None or v < best[0]:
                        best = (v, float(px), float(py), h_)
        _, cx, cy, ch = best
    cw = ch * asp
    cx = float(np.clip(cx, X0 + cw / 2, max(X0 + cw / 2, X1 - cw / 2)))
    cy = float(np.clip(cy, Y0 + ch / 2, max(Y0 + ch / 2, Y1 - ch / 2)))
    return (cx, cy, ch), (X0, Y0, X1, Y1)


def _active(an, a, b, box):
    """The rectangle where a playing video actually moves, in source pixels, or None."""
    mv = an.get('move') or {}
    if not mv.get('cols'):
        return None
    i, j = int(a * mv['rate']), max(int(a * mv['rate']) + 2, int(b * mv['rate']))
    c = np.array(mv['cols'][i:j], np.float32)
    r = np.array(mv['rows'][i:j], np.float32)
    if len(c) < 6:
        return None
    c, r = np.percentile(c, 80, axis=0), np.percentile(r, 80, axis=0)
    if c.sum() < 4:
        return None

    def span(v):
        k = np.flatnonzero(v > max(.22 * np.percentile(v, 85), .25))
        if not len(k):
            return 0.0, 1.0
        best, s = (k[0], k[0]), k[0]                 # longest block, small holes allowed
        for p, q in zip(k, k[1:]):
            if q - p > max(2, len(v) // 12):
                s = q
            if q - s > best[1] - best[0]:
                best = (s, q)
        return best[0] / len(v), (best[1] + 1) / len(v)
    x0, x1 = span(c)
    y0, y1 = span(r)
    if (x1 - x0) * (y1 - y0) > .72 or x1 - x0 < .24 or y1 - y0 < .24:
        return None
    sw, sh = an['meta']['w'], an['meta']['h']
    return (max(box[0], (x0 - .008) * sw), max(box[1], (y0 - .012) * sh), min(box[2], (x1 + .008) * sw), min(box[3], (y1 + .012) * sh))


def plan_split(an, dur, keep=None, whole=False, square=False):
    """Webcam on top, what they are looking at below. Both fixed.

    square: one square picture of what they are looking at, the webcam as a small inset in its corner."""
    sw, sh = an['meta']['w'], an['meta']['h']
    box = _box(an)
    ts, real = _grid(an, dur, keep)
    T = len(ts)
    game = bool(an.get('as_game'))
    share, strength, _ = _moving(an, ts)
    cum = np.concatenate([np.zeros((T, 1)), np.cumsum(share, axis=1)], axis=1)
    edges = np.linspace(0, sw, COLS + 1)
    x, y, w, h = an['layout']['cam']['rect']
    cam_px = (x * sw, y * sh, (x + w) * sw, (y + h) * sh)
    chat = [(a * sw, b * sh, (a + c) * sw, (b + d) * sh) for a, b, c, d in an['layout'].get('chat') or []]
    shots, which = _shots(an, ts, dur)
    states, cols = [], []

    def over(q, a, b, y0, y1):
        return max(0, min(b, q[2]) - max(a, q[0])) * max(0, min(y1, q[3]) - max(y0, q[1])) / ((b - a) * (y1 - y0))

    def add(crops, sel, small):
        for ph, a, b, y0, y1 in crops:
            ia, ib = np.interp([a, b], edges, np.arange(COLS + 1))
            inside = cum[:, min(COLS, int(np.ceil(ib)))] - cum[:, int(ia)]
            c = np.full(T, small[ph])
            c += .8 * strength * np.clip(1 - inside - .18, 0, 1) / .82
            c += 2.4 * over(cam_px, a, b, y0, y1) + min(.14, .7 * sum(over(q, a, b, y0, y1) for q in chat))
            part = over(cam_px, a, b, y0, y1) * (b - a) * (y1 - y0) / max(1, (cam_px[2] - cam_px[0]) * (cam_px[3] - cam_px[1]))
            if part > .02:                       # a second copy of the webcam looks like a mistake
                c += .40
            if game:                             # the crosshair, the car, the character: middle of the screen
                c += .5 * np.clip(abs((a + b) / 2 - sw / 2) / (b - a) - .12, 0, .5)
            col = np.full(T, 9.0)
            col[sel] = c[sel]
            states.append((ph, a, b, y0, y1))
            cols.append(col)
    for n in sorted(set(which.tolist())):
        sel = which == n
        act = _active(an, float(ts[sel][0]), float(ts[sel][-1]) + STEP, box)
        if square:
            if act is not None:
                side = float(min(box[3] - box[1], box[2] - box[0], max(act[2] - act[0], act[3] - act[1])))
                cx = float(np.clip((act[0] + act[2]) / 2, box[0] + side / 2, box[2] - side / 2))
                cy = float(np.clip((act[1] + act[3]) / 2, box[1] + side / 2, box[3] - side / 2))
                add([(SQUARE, cx - side / 2, cx + side / 2, cy - side / 2, cy + side / 2)], sel, {SQUARE: .02})
            add(_crops(box, (SQUARE,)), sel, {SQUARE: .04})
            continue
        if act is not None and not whole and not (game and (act[2] - act[0]) * (act[3] - act[1]) > .55 * sw * sh):
            w_, h_ = act[2] - act[0], act[3] - act[1]
            ph = int(np.clip(1080 * h_ / w_, 520, 1180)) // 2 * 2
            cw, ch = (w_, w_ * ph / 1080) if w_ * ph / 1080 <= h_ + 1 else (h_ * 1080 / ph, h_)
            cx, cy = (act[0] + act[2]) / 2, (act[1] + act[3]) / 2
            add([(ph, cx - cw / 2, cx + cw / 2, cy - ch / 2, cy + ch / 2)], sel, {ph: .02})
        add(_crops(box, (640,) if whole else (1180, 860, 640)), sel, {1180: 0.0, 860: .13, 640: .30} if game else {1180: .10, 860: .09, 640: .17})
    cost = np.stack(cols, axis=1)
    ids = [s[0] for s in states]
    path = _solve(cost * STEP, ids, real, move=2.6, reshape=1.0, at_cut=.6)
    runs = _tidy(_runs(path, real), cost, ids, real)
    cam_box = _cam_shot(an, 740)[1]

    def make(s, a, b):
        ph, x0, x1, y0, y1 = states[s]
        if square:
            return {'kind': 'pip', 'ph': SQUARE, 'cam': _cam_shot(an, PIP)[0], 'crop': ((x0 + x1) / 2, (y0 + y1) / 2, y1 - y0)}
        top = 740 if ph >= 1100 else int(np.clip(1640 - ph, 700, 860)) // 2 * 2
        return {'kind': 'split', 'top': top, 'ph': 1180 if ph >= 1100 else ph, 'cam': _cam_shot(an, top)[0], 'crop': ((x0 + x1) / 2, (y0 + y1) / 2, y1 - y0)}
    return {'mode': 'steady', 'segs': _segments(ts, runs, make, an), 'box': box, 'cam_box': cam_box, 'cost': float(cost[np.arange(T), path].mean()),
            'look': 'square' if square else None}


def _look(an, dur, keep, look, people):
    """One picture, never cut in two: a square of the whole scene, or the full screen when the scene fits it."""
    sq = plan_irl(an, dur, keep, people=people, heights=(SQUARE, 860), fill=False)        # square, a little wider only when people need it
    sq['look'] = 'square'
    if look != 'fill':
        return sq
    full = plan_irl(an, dur, keep, people=people, heights=(1920,), fill=False, stack=True)       # two people far apart: one above the other
    if full['cost'] > sq['cost'] + FIT:
        sq['look'], sq['wanted'] = 'square', 'fill'          # people or the action do not fit a phone-shaped crop
        return sq
    full['look'] = 'fill'
    return full


def cam_seen(an, keep=None):
    """Share of the kept seconds where the webcam actually shows a face."""
    spans = keep or [[0, an['meta']['duration']]]
    ts = np.concatenate([np.arange(a, b, .25) for a, b in spans]) if spans else np.zeros(0)
    ft = np.array(sorted(r[0] for r in an.get('face_cam') or []))
    if not len(ts) or not len(ft):
        return 0.0
    k = np.clip(np.searchsorted(ft, ts), 1, len(ft) - 1) if len(ft) > 1 else np.zeros(len(ts), int)
    near = np.minimum(np.abs(ft[k] - ts), np.abs(ft[np.maximum(k - 1, 0)] - ts))
    return float((near < .6).mean())


def plan(an, override=None, keep=None, look=None):
    """override: None | {'mode': 'split'|'irl'|'scene'|'wide'|'full', 'cam': [x,y,w,h]}
    look: None (older Shorts) | 'square' | 'fill'."""
    dur = an['meta']['duration']
    mode = an['layout']['mode']
    if override:
        an = {**an, 'layout': {**an['layout']}}
        if override.get('cam'):
            an['layout']['cam'] = {'kind': 'box', 'rect': override['cam'], 'conf': 1.0, 'people': 1, 'manual': True}
            x, y, w, h = override['cam']
            an['face_cam'] = [r for r in an['face_cam'] if x <= r[1] + r[3] / 2 <= x + w and y <= r[2] + r[4] / 2 <= y + h]
            mode = 'split'
        mode = override.get('mode') or mode
        if mode == 'split' and not an['layout']['cam']:
            mode = 'irl'
    if mode == 'portrait':
        m = an['meta']
        return {'mode': 'steady', 'segs': [{'a': 0, 'b': dur + 1, 'kind': 'one', 'ph': 1920, 'crop': (m['w'] / 2, m['h'] / 2, min(m['h'], m['w'] * 16 / 9))}],
                'box': (0, 0, m['w'], m['h'])}
    if mode == 'full':
        x0, y0, x1, y1 = box = _box(an)
        return {'mode': 'steady', 'segs': [{'a': 0, 'b': dur + 1, 'kind': 'one', 'ph': 608, 'crop': ((x0 + x1) / 2, (y0 + y1) / 2, min(y1 - y0, (x1 - x0) * 608 / 1080))}],
                'box': box}
    if mode == 'wide':
        if an['layout'].get('cam'):
            return plan_split(an, dur, keep, whole=True)
        if look:                                       # a bigger picture with plain space around it, never cut in two
            return {**plan_irl(an, dur, keep, people=bool(an['tracks']), heights=(860, 700), fill=False), 'look': 'wide'}
        return plan_irl(an, dur, keep, people=bool(an['tracks']), heights=(860, 700), force=True)
    if mode == 'split':
        if look == 'fill' and cam_seen(an, keep) < .7:              # an empty webcam would fill half the phone
            return {**plan_split(an, dur, keep, square=True), 'look': 'square', 'wanted': 'fill'}
        if look:
            return {**plan_split(an, dur, keep, square=look == 'square'), 'look': look}
        return plan_split(an, dur, keep)
    if look:
        return _look(an, dur, keep, look, bool(an['tracks']) and mode != 'scene')
    return plan_irl(an, dur, keep, people=bool(an['tracks']) and mode != 'scene')
