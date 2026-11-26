"""Virtual camera: where to look in the source at every moment of the output.

Planner output is sampled on a 20 Hz grid and interpolated per frame. Crops are
expressed in source pixels as (cx, cy, height); width follows the target aspect.
"""
from __future__ import annotations
import numpy as np
from .audio import smooth

W, H = 1080, 1920
GAME_H = 1180            # split layout: lower panel
CAM_H = H - GAME_H       # split layout: upper panel
DT = .05


def _even_follow(target, dt, dead, speed, start=None, hard=()):
    """Operator-like follow: ignore small moves, glide on real ones, jump on cuts."""
    out = np.zeros(len(target))
    cur = target[0] if start is None else start
    vel, aim = 0.0, cur
    hard = set(hard)
    for i, t in enumerate(target):
        if i in hard:
            cur, vel, aim = t, 0.0, t
        if abs(t - aim) > dead:
            aim = t - np.sign(t - aim) * dead * .5
        acc = speed * speed * (aim - cur) - 2 * speed * vel
        vel += acc * dt
        cur += vel * dt
        out[i] = cur
    return out


def usable(an):
    """Source area left after letterbox bars and (optionally) baked-in captions."""
    L = an['layout']
    top, bot, left, right = L['bars']
    y1 = 1 - bot
    if L['captions']:
        y1 = min(y1, L['captions']['top'])
    return left, top, 1 - right, y1


def _free_interval(an, x0, x1, y0, y1, need):
    """Widest horizontal interval clear of the webcam (and chat when it fits)."""
    iv = [(x0, x1)]
    cam = an['layout']['cam']

    def carve(iv, rect, pad=.004):
        rx, ry, rw, rh = rect
        if ry + rh <= y0 or ry >= y1:
            return iv
        out = []
        for a, b in iv:
            if rx + rw + pad <= a or rx - pad >= b:
                out.append((a, b))
                continue
            if rx - pad > a:
                out.append((a, rx - pad))
            if rx + rw + pad < b:
                out.append((rx + rw + pad, b))
        return out
    base = carve(iv, cam['rect']) if cam else iv
    best = max(base, key=lambda p: p[1] - p[0]) if base else (x0, x1)
    tidy = base
    for c in an['layout']['chat']:
        tidy = carve(tidy, c)
    for p in sorted(tidy, key=lambda p: p[0] - p[1]):
        if p[1] - p[0] >= need * .90 and p[0] < .62 and p[1] > .38:
            return p
    return best


def split_plan(an, dur):
    """Facecam on top, gameplay below."""
    m = an['meta']
    sw, sh = m['w'], m['h']
    cam = an['layout']['cam']
    x, y, w, h = cam['rect']
    ins = 2 / sw if cam['kind'] == 'box' else 0
    cx0, cy0, cx1, cy1 = (x + ins) * sw, (y + ins) * sh, (x + w - ins) * sw, (y + h - ins) * sh
    n = int(dur / DT) + 2
    ts = np.arange(n) * DT
    fc = an['face_cam']
    if fc:
        ft = np.array([r[0] for r in fc])
        fx = np.interp(ts, ft, smooth([(r[1] + r[3] / 2) * sw for r in fc], 5))
        fy = np.interp(ts, ft, smooth([(r[2] + r[4] / 2) * sh for r in fc], 5))
        fh = float(np.median([r[4] for r in fc])) * sh
    else:
        fx, fy = np.full(n, (cx0 + cx1) / 2), np.full(n, cy0 + .42 * (cy1 - cy0))
        fh = .4 * (cy1 - cy0)
    cw, ch = cx1 - cx0, cy1 - cy0
    aspect = W / CAM_H
    if cw / ch >= aspect:
        bh = ch
    else:
        bh = cw / aspect
    # Crop in on a small face so the reaction is readable, never past 1.45x.
    zoom = float(np.clip(.30 * bh / max(fh, 1), 1.0, 1.45)) if cam.get('people', 1) == 1 and cam['kind'] == 'box' else 1.0
    full = bh
    bh = bh / zoom
    # When the streamer stands up, leans out or hides, open up to the whole webcam.
    if fc:
        k = np.clip(np.searchsorted(ft, ts), 0, len(ft) - 1)
        near = np.minimum(np.abs(ft[k] - ts), np.abs(ft[np.maximum(k - 1, 0)] - ts))
        away = smooth((near > .9).astype(float), 25)
    else:
        away = np.ones(n)
    bhs = bh + (full - bh) * away
    fx = fx * (1 - away) + (cx0 + cx1) / 2 * away
    fy = fy * (1 - away) + (cy0 + .42 * (cy1 - cy0)) * away
    cxs = _even_follow(np.clip(fx, cx0 + bhs * aspect / 2, cx1 - bhs * aspect / 2), DT, .10 * bh, 2.6)
    cys = _even_follow(np.clip(fy + .10 * bh, cy0 + bhs / 2, cy1 - bhs / 2), DT, .12 * bh, 2.2)
    # ---- gameplay
    x0, y0, x1, y1 = usable(an)
    ga = W / GAME_H
    gh = (y1 - y0) * sh
    gw = gh * ga
    fr = _free_interval(an, x0, x1, y0, y1, gw / sw)
    free = (fr[1] - fr[0]) * sw
    if gw > free:
        k = max(free / gw, 1 / 1.35)
        gh, gw = gh * k, gw * k
    if gw > (x1 - x0) * sw:
        gw = (x1 - x0) * sw
        gh = gw / ga
    g = an['grid']
    ix, st = np.asarray(an['curves']['ix']), np.asarray(an['curves']['is'])
    tx = (np.arange(len(ix)) + .5) * g
    mid = (fr[0] + fr[1]) / 2 if fr[1] - fr[0] < gw / sw * 1.25 else np.clip(.5, fr[0] + gw / sw / 2, fr[1] - gw / sw / 2)
    tgt = np.interp(ts, tx, smooth(ix * np.clip(st, 0, 1) + mid * (1 - np.clip(st, 0, 1)), 13))
    lo, hi = fr[0] + gw / sw / 2, fr[1] - gw / sw / 2
    if lo > hi:
        lo = hi = (fr[0] + fr[1]) / 2
        lo = hi = float(np.clip(lo, x0 + gw / sw / 2, x1 - gw / sw / 2))
    cuts = [int(c / DT) for c in an['layout']['cuts']]
    gx = _even_follow(np.clip(tgt, lo, hi) * sw, DT, .06 * sw, 1.8, hard=cuts)
    gcy = y0 * sh + gh / 2 + ((y1 - y0) * sh - gh) * .5
    return {'mode': 'split', 'ts': ts, 'cam': (cxs, cys, bhs), 'cam_box': (cx0, cy0, cx1, cy1),
            'game': (gx, np.full(n, gcy), np.full(n, gh)), 'game_box': (x0 * sw, y0 * sh, x1 * sw, y1 * sh), 'face_h': fh}


def _speaking(an, ts):
    talk = np.zeros(len(ts))
    for w, s, e, p in an['speech']['words']:
        talk[(ts >= s - .1) & (ts <= e + .15)] = 1
    return talk


def irl_plan(an, dur):
    """Full-bleed 9:16 for real-world footage: follow, switch, or stack two people."""
    m = an['meta']
    sw, sh = m['w'], m['h']
    x0, y0, x1, y1 = usable(an)
    uh = (y1 - y0) * sh
    uw = (x1 - x0) * sw
    full_h = min(uh, uw * 16 / 9)
    n = int(dur / DT) + 2
    ts = np.arange(n) * DT
    cx, cy, ch = np.full(n, (x0 + x1) / 2 * sw), np.full(n, (y0 + y1) / 2 * sh), np.full(n, full_h)
    mode = np.zeros(n, int)              # 0 single, 1 stacked
    A = np.zeros((n, 3))
    B = np.zeros((n, 3))
    talk = _speaking(an, ts)
    hard = [0]
    shots = list(an['layout']['cuts']) + [dur + 1]
    ix, st = np.asarray(an['curves']['ix']), np.asarray(an['curves']['is'])
    tx = (np.arange(len(ix)) + .5) * an['grid']
    fallback = np.interp(ts, tx, smooth(ix * st + .5 * (1 - st), 15)) * sw
    for a, b in zip(shots, shots[1:]):
        i0, i1 = int(a / DT), min(n, int(np.ceil(min(b, dur) / DT)) + 1)
        if i1 - i0 < 2:
            continue
        hard.append(i0)
        length = max(.1, min(b, dur) - a)
        tr = [t for t in an['tracks'] if abs(t['shot'][0] - a) < .06 and len(t['t']) >= 2]
        for t in tr:
            t['_w'] = float(np.median(t['w']))
            tt_ = np.array(t['t'])
            t['_cov'] = float(min(1.0, (np.minimum(np.diff(tt_, append=tt_[-1] + .3), .9).sum()) / length))
        big = max([t['_w'] for t in tr], default=0)
        tr = [t for t in tr if t['_w'] >= max(.024, .34 * big)]
        if not tr:
            cx[i0:i1] = fallback[i0:i1]
            continue
        main = sorted(tr, key=lambda t: -t['_w'] * min(1, t['_cov']))
        cw = full_h * 9 / 16
        # --- two people too far apart for one portrait crop -> stacked panels,
        #     but only while both are on screen and the camera is not swinging
        if len(main) >= 2 and length >= 1.6:
            p, q = main[0], main[1]
            tt = ts[i0:i1]
            if min(p['_cov'], q['_cov']) > .45 and q['_w'] > .45 * p['_w']:
                def live_pos(t):
                    ta = np.array(t['t'])
                    k = np.clip(np.searchsorted(ta, tt), 0, len(ta) - 1)
                    near = np.minimum(np.abs(ta[k] - tt), np.abs(ta[np.maximum(k - 1, 0)] - tt))
                    return np.interp(tt, ta, smooth(np.array(t['cx']) * sw, 5)), np.interp(tt, ta, smooth(np.array(t['cy']) * sh, 5)), near < .8
                px, py, pl_ = live_pos(p)
                qx, qy, ql_ = live_pos(q)
                gap = np.abs(px - qx)
                hw = (p['_w'] + q['_w']) / 2 * sw
                speed = smooth(np.abs(np.gradient(px, DT)) + np.abs(np.gradient(qx, DT)), 21) / 2 / sw
                want = (pl_ & ql_ & (gap > max(cw * .72, cw * .95 - hw)) & (speed < .22)).astype(float)
                ok = smooth(want, 61) > .52
                # Stay stacked through short interruptions instead of flipping back and forth.
                idx = np.flatnonzero(ok)
                for u, v in zip(idx, idx[1:]):
                    if 1 < v - u <= int(2.6 / DT):
                        ok[u:v] = True
                # runs shorter than 2.2 s are not worth a layout change
                a0 = None
                for k in range(len(ok) + 1):
                    v = ok[k] if k < len(ok) else False
                    if v and a0 is None:
                        a0 = k
                    elif not v and a0 is not None:
                        if (k - a0) * DT >= min(3.4, length * .85):
                            sel = slice(a0, k)
                            g_ = float(np.median(gap[sel]))
                            pw = float(np.clip(max(p['_w'], q['_w']) * sw * 3.3, .20 * sw, min(.5 * sw, max(g_, 1.5 * hw))))
                            ph = pw / (W / (H / 2))
                            if ph > uh:
                                ph, pw = uh, uh * W / (H / 2)
                            first = px[sel].mean() < qx[sel].mean()
                            for (xx, yy), dst in (((px, py) if first else (qx, qy), A), ((qx, qy) if first else (px, py), B)):
                                xs = np.clip(xx[sel], x0 * sw + pw / 2, x1 * sw - pw / 2)
                                ys = np.clip(yy[sel] + .10 * ph, y0 * sh + ph / 2, y1 * sh - ph / 2)
                                dst[i0 + a0:i0 + k, 0] = _even_follow(xs, DT, .08 * pw, 2.4)
                                dst[i0 + a0:i0 + k, 1] = _even_follow(ys, DT, .09 * ph, 1.9)
                                dst[i0 + a0:i0 + k, 2] = ph
                            mode[i0 + a0:i0 + k] = 1
                            hard += [i0 + a0, i0 + k]
                        a0 = None
        # --- one camera that follows whoever matters
        K = len(tr)
        sc = np.full((K, i1 - i0), -9.0)
        pos = np.zeros((K, i1 - i0))
        ypos = np.zeros((K, i1 - i0))
        siz = np.zeros((K, i1 - i0))
        away = np.full((K, i1 - i0), 99.0)
        hold = np.array([3.5 if np.std(t['cx']) < .08 else 1.2 for t in tr])
        for k, t in enumerate(tr):
            tt = ts[i0:i1]
            tarr = np.array(t['t'])
            idx = np.clip(np.searchsorted(tarr, tt), 0, len(tarr) - 1)
            near = np.minimum(np.abs(tarr[idx] - tt), np.abs(tarr[np.maximum(idx - 1, 0)] - tt))
            live = near < .95
            away[k] = near
            pos[k] = np.interp(tt, t['t'], smooth(np.array(t['cx']) * sw, 5))
            ypos[k] = np.interp(tt, t['t'], smooth(np.array(t['cy']) * sh, 5))
            siz[k] = np.interp(tt, t['t'], t['w'])
            act = np.interp(tt, t['act_t'], t['act'], left=0, right=0) if t['act'] else 0
            edge = np.clip((.11 - np.minimum(pos[k] / sw, 1 - pos[k] / sw)) / .11, 0, 1)
            s = .8 * np.minimum(siz[k] / max(big, 1e-4), 1.15) + .9 * t['_cov'] - .8 * edge + .16 * np.clip(act, -2, 8) * (0.35 + .65 * talk[i0:i1])
            # The main person, missed for a moment, still outranks a brief stray face.
            kept = (near < hold[k]) & (t['_cov'] > .25) & (hold[k] > 2)
            sc[k] = np.where(live, smooth(s, 11), np.where(kept, smooth(s, 11) - .4, -9))
        cur, since, choice = int(np.argmax(sc[:, 0])), 0, np.zeros(i1 - i0, int)
        for j in range(i1 - i0):
            best = int(np.argmax(sc[:, j]))
            lost = sc[cur, j] < -5
            if best != cur and sc[best, j] > -5 and (lost or (since * DT > 3.2 and sc[best, j] > sc[cur, j] + .50)):
                if abs(pos[best, j] - pos[cur, j]) > .22 * cw and (since * DT > .5 or lost):
                    hard.append(i0 + j)
                cur, since = best, 0
            choice[j] = cur
            since += 1
        # A face that is only missed for a moment keeps the camera; do not wander off to the furniture.
        for j in np.flatnonzero(sc.max(axis=0) < -5):
            k = int(np.argmin(away[:, j] - hold))
            if away[k, j] <= hold[k]:
                choice[j] = k
        gx = pos[choice, np.arange(i1 - i0)].copy()
        gy = ypos[choice, np.arange(i1 - i0)].copy()
        # Nobody detectable for a while: drift to where the movement is.
        gone = smooth(((sc.max(axis=0) < -5) & ((away - hold[:, None]).min(axis=0) > 0)).astype(float), 9)
        gx = gx * (1 - gone) + fallback[i0:i1] * gone
        gy = gy * (1 - gone) + (y0 + .46 * (y1 - y0)) * sh * gone
        # Pull in a neighbour's face when both fit comfortably.
        for j in range(i1 - i0):
            near = [k for k in range(K) if sc[k, j] > -5 and k != choice[j] and abs(pos[k, j] - gx[j]) < cw * .62]
            if near:
                pts = [gx[j]] + [pos[k, j] for k in near]
                mid = (min(pts) + max(pts)) / 2
                if max(pts) - min(pts) < cw * .70:
                    gx[j] = .55 * mid + .45 * gx[j]
        fw = float(np.median(siz[choice, np.arange(i1 - i0)]))
        z = float(np.clip(.21 / max(fw * sw / cw, 1e-3), 1.0, 1.42))
        hh = full_h / z
        ww = hh * 9 / 16
        local_hard = [h_ - i0 for h_ in hard if i0 <= h_ < i1]
        cx[i0:i1] = _even_follow(np.clip(gx, x0 * sw + ww / 2, x1 * sw - ww / 2), DT, .10 * ww, 2.3, hard=local_hard)
        yt = np.clip(gy + .13 * hh, y0 * sh + hh / 2, y1 * sh - hh / 2)
        cy[i0:i1] = _even_follow(yt, DT, .07 * hh, 1.8, hard=local_hard)
        ch[i0:i1] = hh
    lim = np.maximum(0, ch * 9 / 32)
    cx = np.clip(cx, x0 * sw + lim, x1 * sw - lim)
    return {'mode': 'irl', 'ts': ts, 'single': (cx, cy, ch), 'stack': mode, 'A': A, 'B': B, 'hard': sorted(set(hard)),
            'box': (x0 * sw, y0 * sh, x1 * sw, y1 * sh)}


def scene_plan(an, dur):
    """No webcam and no faces: follow the action in a full-height portrait crop."""
    m = an['meta']
    sw, sh = m['w'], m['h']
    x0, y0, x1, y1 = usable(an)
    n = int(dur / DT) + 2
    ts = np.arange(n) * DT
    hh = min((y1 - y0) * sh, (x1 - x0) * sw * 16 / 9)
    ww = hh * 9 / 16
    ix, st = np.asarray(an['curves']['ix']), np.asarray(an['curves']['is'])
    tx = (np.arange(len(ix)) + .5) * an['grid']
    tgt = np.interp(ts, tx, smooth(ix * st + .5 * (1 - st), 21)) * sw
    lo, hi = x0 * sw + ww / 2, x1 * sw - ww / 2
    for c in an['layout']['chat']:
        if c[0] < .12 and c[2] < .3 and (c[0] + c[2]) * sw + ww < x1 * sw:
            lo = max(lo, min((c[0] + c[2]) * sw + ww / 2, .5 * sw))
        elif c[0] + c[2] > .88 and c[2] < .3 and c[0] * sw - ww > x0 * sw:
            hi = min(hi, max(c[0] * sw - ww / 2, .5 * sw))
    cuts = [int(c / DT) for c in an['layout']['cuts']]
    cx = _even_follow(np.clip(tgt, lo, hi), DT, .06 * sw, 1.7, hard=cuts)
    return {'mode': 'irl', 'ts': ts, 'single': (cx, np.full(n, (y0 + y1) / 2 * sh), np.full(n, hh)), 'stack': np.zeros(n, int),
            'A': np.zeros((n, 3)), 'B': np.zeros((n, 3)), 'hard': [0] + cuts, 'box': (x0 * sw, y0 * sh, x1 * sw, y1 * sh)}


def portrait_plan(an, dur):
    m = an['meta']
    n = int(dur / DT) + 2
    hh = min(m['h'], m['w'] * 16 / 9)
    c = lambda v: np.full(n, float(v))
    return {'mode': 'irl', 'ts': np.arange(n) * DT, 'single': (c(m['w'] / 2), c(m['h'] / 2), c(hh)), 'stack': np.zeros(n, int),
            'A': np.zeros((n, 3)), 'B': np.zeros((n, 3)), 'hard': [0], 'box': (0, 0, m['w'], m['h'])}


def plan(an, override=None):
    """override: None | {'mode': 'split'|'irl'|'scene'|'full', 'cam': [x,y,w,h]}"""
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
        return portrait_plan(an, dur)
    if mode == 'split':
        return split_plan(an, dur)
    if mode == 'irl' and an['tracks']:
        return irl_plan(an, dur)
    return scene_plan(an, dur)
