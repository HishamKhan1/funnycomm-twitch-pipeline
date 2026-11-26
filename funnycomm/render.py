"""Frame-accurate compositor: one decode, one encode, every effect in between.

Replaces the old trim -> verticalise -> decorate -> concat chain (four lossy
generations) with a single pass at the source frame rate.
"""
from __future__ import annotations
import math, queue, subprocess, tempfile, threading, wave
from pathlib import Path
import cv2
import numpy as np
from . import camera, framing, gfx, media, sfx, vision
from .camera import W, H, CAM_H, DT

SR = 48000
ZIN, ZHOLD, ZOUT = .22, 1.1, .38                  # a quick push-in on a reaction, held, then back out


# ------------------------------------------------------------------- easing
def _smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _hold(t, up, hold, down):
    """0->1->0 envelope starting at t=0."""
    if t < 0 or t > up + hold + down:
        return 0.0
    if t < up:
        return 1 - (1 - t / up) ** 3
    if t < up + hold:
        return 1.0
    return 1 - _smooth((t - up - hold) / down)


# ------------------------------------------------------------------ decoding
class Reader:
    """Raw BGR frames for one source range at the output frame rate."""

    def __init__(self, path, meta, a, b, fps, speed=1.0, clean='', size=None):
        self.w, self.h = meta['w'], meta['h']
        fit = f'scale={self.w}:{self.h}:flags=lanczos:in_color_matrix=bt709:in_range=tv' if size and size != (self.w, self.h) else 'scale=in_color_matrix=bt709:in_range=tv'
        vf = f"setpts=(PTS-STARTPTS)/{speed:.5f},fps={fps},{clean + ',' if clean else ''}{fit},format=bgr24"
        self.p = subprocess.Popen([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-ss', f'{a:.4f}', '-t', f'{b - a + .08:.4f}', '-i', str(path),
                                   '-an', '-vf', vf, '-f', 'rawvideo', 'pipe:1'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  bufsize=self.w * self.h * 3, creationflags=media.NOWIN)
        self.last = None
        self.q = queue.Queue(maxsize=6)
        self.stop = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        n = self.w * self.h * 3
        try:
            while not self.stop:
                buf = self.p.stdout.read(n)
                if len(buf) < n:
                    break
                self.q.put(np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3))
        except (OSError, ValueError):
            pass
        self.q.put(None)

    def next(self):
        if self.last is not False:
            f = self.q.get()
            if f is None:
                if self.last is None:
                    raise media.MediaError('A source clip produced no frames.')
                self.q.put(None)
                return self.last
            self.last = f
        return self.last

    def close(self):
        self.stop = True
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        if self.p.poll() is None:
            self.p.kill()
        self.p.stdout.close()
        self.p.wait()


class Writer:
    def __init__(self, target, fps, preset='faster', crf=18, size=(W, H)):
        big = size[1] > 2000
        self.err = tempfile.TemporaryFile()
        self.p = subprocess.Popen([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{size[0]}x{size[1]}',
                                   '-r', str(fps), '-i', 'pipe:0', '-an', '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p',
                                   '-c:v', 'libx264', '-preset', preset, '-crf', str(crf), '-maxrate', '30M' if big else '18M', '-bufsize', '48M' if big else '30M', '-profile:v', 'high',
                                   '-g', str(int(fps * 2)), '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
                                   str(target)], stdin=subprocess.PIPE, stderr=self.err, creationflags=media.NOWIN)
        self.q = queue.Queue(maxsize=6)
        self.bad = None
        self.t = threading.Thread(target=self._pump, daemon=True)
        self.t.start()

    def _pump(self):
        while True:
            f = self.q.get()
            if f is None:
                break
            if self.bad is None:
                try:
                    self.p.stdin.write(f.data)
                except (OSError, ValueError) as e:
                    self.bad = e

    def write(self, f):
        if self.bad is not None or self.p.poll() is not None:
            self.err.seek(0)
            raise media.MediaError('Video encoder stopped: ' + self.err.read().decode('utf-8', 'replace')[-300:])
        self.q.put(f)

    def close(self):
        self.q.put(None)
        self.t.join()
        try:
            self.p.stdin.close()
        except OSError:
            pass
        if self.p.wait():
            self.err.seek(0)
            raise media.MediaError('Video encoder failed: ' + self.err.read().decode('utf-8', 'replace')[-300:])

    def abort(self):
        try:
            self.p.kill()
            self.p.wait()
        finally:
            self.q.put(None)


# ---------------------------------------------------------------- geometry
def warp(src, cx, cy, ch, dw, dh, box=None):
    """Sub-pixel crop of height ch centred at (cx,cy) -> dw x dh."""
    s = ch / dh
    cw = dw * s
    if box is not None:
        x0, y0, x1, y1 = box
        if cw > x1 - x0:
            s = (x1 - x0) / dw
            cw, ch = dw * s, dh * s
        if ch > y1 - y0:
            s = (y1 - y0) / dh
            cw, ch = dw * s, dh * s
        cx = min(max(cx, x0 + cw / 2), x1 - cw / 2)
        cy = min(max(cy, y0 + ch / 2), y1 - ch / 2)
    M = np.array([[s, 0, cx - cw / 2 + (s - 1) / 2], [0, s, cy - ch / 2 + (s - 1) / 2]], np.float64)
    interp = cv2.INTER_CUBIC if s < .8 else cv2.INTER_LINEAR
    out = cv2.warpAffine(src, M, (dw, dh), flags=interp | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
    if s < .62:
        out = cv2.addWeighted(out, 1.38, cv2.GaussianBlur(out, (0, 0), 1.5), -.38, 0)
    return out


def shot(src, cx, cy, ch, dw, dh, box=None, sharp=True):
    """A fixed crop, resampled as cleanly as the source allows."""
    cw = ch * dw / dh
    H_, W_ = src.shape[:2]
    if box is not None:
        x0, y0, x1, y1 = box
        k = min(1.0, (x1 - x0) / cw, (y1 - y0) / ch)
        cw, ch = cw * k, ch * k
        cx = min(max(cx, x0 + cw / 2), x1 - cw / 2)
        cy = min(max(cy, y0 + ch / 2), y1 - ch / 2)
    a, b = int(round(max(0, cx - cw / 2))), int(round(max(0, cy - ch / 2)))
    c, d = int(round(min(W_, cx + cw / 2))), int(round(min(H_, cy + ch / 2)))
    roi = src[b:d, a:c]
    up = dw / max(1, c - a)
    if up <= .95:
        out = cv2.resize(roi, (dw, dh), interpolation=cv2.INTER_AREA)
    else:
        out = cv2.resize(roi, (dw, dh), interpolation=cv2.INTER_LANCZOS4 if up < 2.6 else cv2.INTER_CUBIC)
    if sharp and up > .8:
        # Give back the edge contrast that stretching takes away; more stretch, wider radius.
        sg = float(np.clip(.62 * up, .7, 1.7))
        am = float(np.clip(.26 + .22 * (up - 1), .22, .58))
        out = cv2.addWeighted(out, 1 + am, cv2.GaussianBlur(out, (0, 0), sg), -am, 0)
    return out


def pixelate(im):
    h, w = im.shape[:2]
    s = cv2.resize(im, (max(4, w // 72), max(4, h // 72)), interpolation=cv2.INTER_AREA)
    out = cv2.resize(s, (w, h), interpolation=cv2.INTER_NEAREST)
    return cv2.GaussianBlur(out, (0, 0), 9)


def _face_at(an, t):
    """Is a readable face on screen around t? Replays of blur or backs of heads are noise."""
    if an['layout']['cam']:
        return sum(1 for r in an['face_cam'] if abs(r[0] - t) < .6) >= 2
    return any(abs(u - t) < .35 and w > .07 for tr in an['tracks'] for u, w in zip(tr['t'], tr['w']))


def soften(src, rects, crop):
    """Blur stream-chat text that falls inside the crop (often half cut off, sometimes offensive)."""
    H_, W_ = src.shape[:2]
    cx0, cy0, cx1, cy1 = crop
    out = None
    for x, y, w, h in rects:
        x0, y0, x1, y1 = int(max(0, x * W_ - 10)), int(max(0, y * H_ - 10)), int(min(W_, (x + w) * W_ + 14)), int(min(H_, (y + h) * H_ + 12))
        if x1 <= cx0 or x0 >= cx1 or y1 <= cy0 or y0 >= cy1 or x1 - x0 < 8 or y1 - y0 < 8:
            continue
        if out is None:
            out = src.copy()
        roi = out[y0:y1, x0:x1]
        hh, ww = roi.shape[:2]
        # A wide median wipes out thin letters but keeps the people and shapes behind them.
        q = 3
        sm = cv2.resize(roi, (max(8, ww // q), max(8, hh // q)), interpolation=cv2.INTER_AREA)
        pad = 24
        big = cv2.copyMakeBorder(sm, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
        big = cv2.medianBlur(big, 15)
        big = cv2.GaussianBlur(big, (0, 0), 1.7)
        sm = big[pad:-pad, pad:-pad].astype(np.float32)
        bl = cv2.resize(sm, (ww, hh), interpolation=cv2.INTER_LINEAR)
        m = np.ones((hh, ww), np.float32)
        f = max(1, min(26, hh // 3, ww // 3))
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        if x0 > 0:
            m[:, :f] *= ramp
        if x1 < W_:
            m[:, -f:] *= ramp[::-1]
        if y0 > 0:
            m[:f] *= ramp[:, None]
        if y1 < H_:
            m[-f:] *= ramp[::-1][:, None]
        m = m[:, :, None]
        roi[:] = (bl * m + roi * (1 - m)).astype(np.uint8)
    return src if out is None else out


def _in(spans, t):
    return any(a <= t <= b for a, b in spans or [])


class Scene:
    """Composes clean (overlay-free) vertical frames for one source clip."""

    def __init__(self, item, an, style, size=(W, H)):
        self.item, self.an, self.style = item, an, style
        self.ow, self.oh = size
        self.k = size[0] / W                     # output pixels per design pixel
        self.cut = item['cut']
        if style.get('steady', True):
            self.plan = framing.plan(an, item.get('layout'), self.cut['keep'], style.get('look'))
            self.ts = np.zeros(1)
        else:
            self.plan = camera.plan(an, item.get('layout'))
            self.ts = self.plan['ts']
        self.bars = False
        self.head_y = None
        self.payoff = self.cut['payoff']
        beats = [(b['t'], b['s']) for b in an['moments']['beats']]
        if not any(abs(t - self.payoff) < .3 for t, _ in beats):
            beats.append((self.payoff, 1.0))
        keep = self.cut['keep']
        self.beats = sorted((t, s) for t, s in beats if any(a - .1 <= t <= b for a, b in keep))
        self.energy = style.get('energy', 1.0)
        cb = self.plan.get('cam_box')
        self.expand_ok = bool(cb) and (cb[3] - cb[1]) >= 200 and style.get('reaction_expand', True)
        self.loud = float(an['audio'].get('peak_gap_db') or 0)
        self.seam = CAM_H
        self.censored = False
        self.start = keep[0][0]
        self.chat = an['layout']['chat'] if style.get('blur_chat', True) else []
        self.look = self.plan.get('look')
        self.pic = None                          # (top, height) of a picture with plain space around it
        self.zooms = self._zooms()
        self.cap_high = self._faces_low() > .3   # faces sit where the caption would go: put it higher

    def _faces_low(self):
        """Share of the kept time where faces sit in the lower part of the picture."""
        if not self.look or self.plan.get('mode') != 'steady':
            return 0.0
        sw, sh = self.an['meta']['w'], self.an['meta']['h']
        hit = tot = 0.0
        for g in self.plan['segs']:
            if g['kind'] != 'one':
                continue
            cx, cy, ch = g['crop']
            cw = ch * 1080 / g['ph']
            for tr in self.an['tracks']:
                for u, x, y, w in zip(tr['t'], tr['cx'], tr['cy'], tr['w']):
                    if g['a'] <= u < g['b'] and _in(self.cut['keep'], u) and abs(x * sw - cx) < cw / 2 and w * sw > .06 * cw:
                        tot += 1
                        hit += (y * sh - (cy - ch / 2)) / ch > .62
        return hit / tot if tot else 0.0

    # ------------------------------------------------------------ reaction zooms
    def _zooms(self):
        """At most two quick push-ins on a face reacting, at the biggest moments. Most Shorts get one or none."""
        if not self.look or not self.style.get('zoom', True) or self.plan.get('mode') != 'steady':
            return []
        an, keep = self.an, self.cut['keep']
        total = sum(b - a for a, b in keep)
        if an['moments']['peak'] < 1.1:
            return []                            # only the biggest reactions get one; most Shorts stay still
        want = 2 if total > 25 and self.loud > 18 else 1
        cands = [(self.payoff, 9.0)] + sorted(((t, s) for t, s in self.beats if s >= .8 and abs(t - self.payoff) > .3), key=lambda q: -q[1])
        out = []
        for t, _ in cands:
            if len(out) >= want:
                break
            t0 = t + .05
            t1 = t0 + ZIN + ZHOLD + ZOUT
            if any(abs(t0 - e['t0']) < 5 for e in out) or not any(a <= t0 and t1 <= b for a, b in keep):
                continue
            n0, g = self._seg(t0)
            if self._seg(t1)[0] != n0 or g['kind'] not in ('one', 'split'):
                continue
            if any(t0 - .1 < c < t1 + .1 for c in an['layout']['cuts']):
                continue
            z = self._target(g, t0 + ZIN)
            if z:
                out.append({'t0': t0, 'n': n0, **z})
        return out

    def _target(self, g, t):
        """The face to push in on at t, inside the shot, or None."""
        sw, sh = self.an['meta']['w'], self.an['meta']['h']
        best = None
        if g['kind'] == 'split':
            cx, cy, ch = g['cam']
            cw = ch * 1080 / g['top']
            for r in self.an['face_cam']:
                if abs(r[0] - t) < .4:
                    fx, fy, fw = (r[1] + r[3] / 2) * sw, (r[2] + r[4] / 2) * sh, r[3] * sw
                    if abs(fx - cx) < cw / 2 and (best is None or fw > best[2]):
                        best = (fx, fy, fw)
            return {'k': 1.22, 'fx': best[0], 'fy': best[1]} if best and best[2] < .5 * cw else None
        cx, cy, ch = g['crop']
        cw = ch * 1080 / g['ph']
        for tr in self.an['tracks']:
            for u, x, y, w in zip(tr['t'], tr['cx'], tr['cy'], tr['w']):
                if abs(u - t) < .35:
                    fx, fy, fw = x * sw, y * sh, w * sw
                    if abs(fx - cx) < cw / 2 - .4 * fw and abs(fy - cy) < ch / 2 and (best is None or fw > best[2]):
                        best = (fx, fy, fw)
        if not best or best[2] > .42 * cw or best[2] < .035 * sw:
            return None
        return {'k': float(np.clip(.30 * cw / best[2], 1.25, 1.6)), 'fx': best[0], 'fy': best[1]}

    def _zoomed(self, n, t, crop):
        for e in self.zooms:
            env = _hold(t - e['t0'], ZIN, ZHOLD, ZOUT) if e['n'] == n else 0.0
            if env <= 0:
                continue
            cx, cy, ch = crop
            K = e['k']
            fx, fy = e['fx'], e['fy'] + .05 * ch / K
            gx, gy = (fx + (cx - fx) / K + fx) / 2, (fy + (cy - fy) / K + fy) / 2      # face held, then drawn toward the middle
            return cx + (gx - cx) * env, cy + (gy - cy) * env, ch / (1 + (K - 1) * env)
        return crop

    def _i(self, arr, t):
        k = t / DT
        i = int(k)
        n = len(arr)
        if i >= n - 1:
            return arr[-1]
        if i < 0:
            return arr[0]
        f = k - i
        a, b = arr[i], arr[i + 1]
        return a + (b - a) * f

    def _hardi(self, arr, t):
        """Interpolate but never across a camera cut."""
        k = t / DT
        i = min(max(int(k), 0), len(arr) - 1)
        j = min(i + 1, len(arr) - 1)
        if j in self._hard:
            return arr[i]
        f = k - int(k)
        return arr[i] + (arr[j] - arr[i]) * f

    def punch(self, t):
        # One slow lean-in on the punchline. Extra beats only when the edit is set to high energy.
        amp = .30 * self.energy
        z = amp * _hold(t - (self.payoff - .12), .40 if self.energy < .8 else .16, 1.5, .75)
        if self.energy >= .8:
            for bt, s in self.beats:
                if abs(bt - self.payoff) >= .31:
                    z = max(z, .15 * s * self.energy * _hold(t - (bt - .10), .16, .55, .45))
        return z

    def shake(self, t):
        d = t - self.payoff
        if self.loud < 11 or not 0 <= d < .42 or not self.style.get('shake', True):
            return 0.0, 0.0
        a = 10 * self.energy * (1 - d / .42) ** 2
        return a * math.sin(d * 71), a * math.cos(d * 57)

    def push(self, t):
        """Slow tension push-in from the setup to the payoff."""
        span = max(1.0, self.payoff - self.start)
        if t <= self.payoff:
            return .055 * self.energy * _smooth((t - self.start) / span)
        return .055 * self.energy * (1 - _smooth((t - self.payoff - 1.2) / .8))

    def frame(self, src, t, jump=0, role='main'):
        self._hard = self.plan.get('hard', ())
        if self.plan['mode'] == 'steady':
            return self._steady(src, t)
        fr = self._split(src, t, jump, role) if self.plan['mode'] == 'split' else self._irl(src, t, jump, role)
        return fr if self.k == 1 else cv2.resize(fr, (self.ow, self.oh), interpolation=cv2.INTER_CUBIC)

    def _seg(self, t):
        segs = self.plan['segs']
        for n, g in enumerate(segs):
            if t < g['b']:
                return n, g
        return len(segs) - 1, segs[-1]

    def _steady(self, src, t):
        n, g = self._seg(t)
        k, ow, oh = self.k, self.ow, self.oh
        px = lambda v: int(round(v * k / 2)) * 2
        box = self.plan['box']
        raw = src
        if self.chat:
            src = soften(src, self.chat, (0, 0, src.shape[1], src.shape[0]))
        self.censored = False
        self.bars, self.head_y, self.tag_y = False, None, 1470
        if g['kind'] == 'stack':
            self.seam = H // 2
            out = np.empty((oh, ow, 3), np.uint8)
            half = oh // 2
            for c, y in ((g['A'], 0), (g['B'], half)):
                out[y:y + half] = shot(src, *c, ow, half, box)
            self._line(out, half)
            return out
        if g['kind'] == 'duo':
            self.seam = g['top']
            y = px(g['top'])
            out = np.empty((oh, ow, 3), np.uint8)
            out[:y] = shot(src, *g['wide'], ow, y, box)
            out[y:] = shot(src, *g['crop'], ow, oh - y, box)
            self._line(out, y)
            return out
        self.pic = None
        if g['kind'] == 'one':
            self.seam = 0
            ph = px(g['ph'])
            crop = self._zoomed(n, t, g['crop'])
            if ph >= oh:
                return shot(src, *crop, ow, oh, box)
            out = self._back()
            y = int(np.clip(px(960 if self.look else 930) - ph // 2, 0, oh - ph)) // 2 * 2
            out[y:y + ph] = shot(src, *crop, ow, ph, box)
            self.bars, self.head_y, self.tag_y = True, y / k, (y + ph) / k + 16
            self.pic = (y / k, ph / k)
            return out
        if g['kind'] == 'pip':
            # what they are looking at as one square picture, their webcam small in its corner
            self.seam = 0
            self.censored = _in(self.item.get('censor'), t) and self.style.get('censor', True)
            ph = px(g['ph'])
            out = self._back()
            y = int(np.clip(px(960) - ph // 2, 0, oh - ph)) // 2 * 2
            low = shot(src, *g['crop'], ow, ph, box)
            out[y:y + ph] = pixelate(low) if self.censored else low
            iw, ih = px(380), px(300)
            out[y:y + ih, :iw] = shot(raw, *g['cam'], iw, ih, self.plan['cam_box'])
            e = max(2, int(round(3 * k)))
            out[y:y + ih + e, iw:iw + e] = (8, 8, 8)
            out[y + ih:y + ih + e, :iw + e] = (8, 8, 8)
            self.bars, self.head_y, self.tag_y = True, y / k, (y + ph) / k + 16
            self.pic = (y / k, ph / k)
            return out
        # webcam over the thing they are looking at
        self.censored = _in(self.item.get('censor'), t) and self.style.get('censor', True)
        top, ph = px(g['top']), px(g['ph'])
        if top + ph >= oh - 4:
            ph = oh - top
            out = np.empty((oh, ow, 3), np.uint8)
            y = 0
        else:
            out = self._back()
            y = int(np.clip(px(950) - (top + ph) // 2, 0, oh - top - ph)) // 2 * 2
            self.bars, self.head_y = True, y / k
        out[y:y + top] = shot(raw, *self._zoomed(n, t, g['cam']), ow, top, self.plan['cam_box'])
        low = shot(src, *g['crop'], ow, ph, box)
        out[y + top:y + top + ph] = pixelate(low) if self.censored else low
        self.seam = (y + top) / k
        self._line(out, y + top)
        return out

    def _back(self):
        """Plain dark surround, never blur."""
        return np.full((self.oh, self.ow, 3), (8, 8, 8) if self.look else (22, 16, 13), np.uint8)

    def _line(self, out, y):
        a, b = max(1, int(round(4 * self.k))), max(1, int(round(1.5 * self.k)))
        out[y - a:y + a] = (20, 13, 9)
        out[y - b:y + b] = (143, 227, 43)

    def _split(self, src, t, jump, role):
        z = self.punch(t)
        sx, sy = self.shake(t)
        seam = CAM_H
        self.censored = _in(self.item.get('censor'), t) and self.style.get('censor', True)
        if self.expand_ok:
            seam += 190 * self.energy * _hold(t - (self.payoff - .12), .45 if self.energy < .8 else .24, 1.6, .65)
            if role == 'replay':
                seam = 1120
        if self.censored:
            seam = max(seam, 1010)
        # Censor spans ease in so the panel does not jump.
        if not self.censored:
            for a, b in self.item.get('censor') or []:
                if 0 < a - t < .25:
                    seam = max(seam, CAM_H + (1010 - CAM_H) * _smooth(1 - (a - t) / .25))
                elif 0 < t - b < .3:
                    seam = max(seam, CAM_H + (1010 - CAM_H) * _smooth(1 - (t - b) / .3))
        seam = int(round(min(seam, 1180) / 2) * 2)
        self.seam = seam
        cx, cy, ch = (float(self._i(a, t)) for a in self.plan['cam'])
        # Taller panel keeps the same horizontal field until the box runs out.
        need = ch * seam / CAM_H
        cb = self.plan['cam_box']
        ch2 = min(need, cb[3] - cb[1])
        k = 1 + z + (.12 if role == 'replay' else 0) + (.03 if jump else 0)
        top = warp(src, cx + sx * .25, cy - .02 * ch2 + sy * .25, ch2 / k, W, seam, cb)
        gh = H - seam
        gx, gy, gch = (float(self._i(a, t)) for a in self.plan['game'])
        gk = 1 + self.push(t) + (.05 if jump else 0) + .25 * z
        if self.chat:
            hw_, hh_ = gch * 1080 / camera.GAME_H / 2, gch / 2
            src = soften(src, self.chat, (gx - hw_, gy - hh_, gx + hw_, gy + hh_))
        bot = warp(src, gx + sx, gy + sy, gch * gh / camera.GAME_H / gk, W, gh, self.plan['game_box'])
        if self.censored:
            bot = pixelate(bot)
        out = np.empty((H, W, 3), np.uint8)
        out[:seam] = top
        out[seam:] = bot
        out[seam - 4:seam + 4] = (20, 13, 9)
        out[seam - 1:seam + 2] = (143, 227, 43)
        return out

    def _irl(self, src, t, jump, role):
        self.censored = False
        z = self.punch(t)
        sx, sy = self.shake(t)
        i = min(max(int(t / DT), 0), len(self.ts) - 1)
        box = self.plan['box']
        if self.plan['stack'][i]:
            self.seam = H // 2
            if self.chat:
                src = soften(src, self.chat, (0, 0, src.shape[1], src.shape[0]))
            out = np.empty((H, W, 3), np.uint8)
            k = 1 + .8 * z + (.04 if jump else 0)
            for arr, y in ((self.plan['A'], 0), (self.plan['B'], H // 2)):
                c = self._hardi(arr, t)
                out[y:y + H // 2] = warp(src, float(c[0]) + sx * .4, float(c[1]) + sy * .4, float(c[2]) / k, W, H // 2, box)
            out[H // 2 - 4:H // 2 + 4] = (20, 13, 9)
            out[H // 2 - 1:H // 2 + 2] = (143, 227, 43)
            return out
        self.seam = 0
        cx, cy, ch = (float(self._hardi(a, t)) for a in self.plan['single'])
        if self.chat:
            src = soften(src, self.chat, (cx - ch * 9 / 32, cy - ch / 2, cx + ch * 9 / 32, cy + ch / 2))
        k = 1 + self.push(t) + z * .85 + (.06 if jump else 0) + (.18 if role == 'replay' else 0)
        return warp(src, cx + sx, cy - .06 * ch * (k - 1) + sy, ch / k, W, H, box)


# ------------------------------------------------------------- shot building
def build_shots(plan, ans):
    """Expand the edit plan into ordered output shots."""
    shots = []
    style = plan['style']
    ranked = plan['kind'] == 'ranking'
    for i, it in enumerate(plan['items']):
        keep = it['cut']['keep']
        an = ans[i]
        payoff = it['cut']['payoff']
        total = sum(b - a for a, b in keep)
        pre = sum(min(b, payoff) - a for a, b in keep if a < payoff)
        dur = an['meta']['duration']
        if not ranked and it.get('teaser', 'auto') is not False and style.get('teaser', True):
            want = it.get('teaser') is True or (pre > 9 and total > 16)
            a, b = max(0, payoff - .95), min(dur, payoff + .12)
            if want and b - a > .6 and not _in(it.get('censor'), payoff):
                shots.append({'i': i, 'a': a, 'b': b, 'speed': 1.0, 'role': 'teaser'})
        for k, (a, b) in enumerate(keep):
            shots.append({'i': i, 'a': a, 'b': b, 'speed': 1.0, 'role': 'main', 'jump': k % 2, 'seg': k, 'segs': len(keep)})
        if not ranked and it.get('replay', 'auto') is not False and style.get('replay', True):
            rel = (payoff - keep[0][0]) / max(.1, total)
            want = it.get('replay') is True or (total < 11.5 and rel > .35 and an['moments']['peak'] > .85 and _face_at(an, payoff))
            a, b = max(keep[0][0], payoff - .5), min(keep[-1][1], payoff + .6)
            if want and b - a > .7 and not _in(it.get('censor'), payoff):
                shots.append({'i': i, 'a': a, 'b': b, 'speed': .5, 'role': 'replay'})
    return shots


# ------------------------------------------------------------------ audio
def shot_audio(sh, it, an, samples, style):
    n = int(samples)
    if not an['meta']['audio']:
        return np.zeros((n, 2), np.float32)
    flt = None
    if sh['speed'] != 1:
        # Tape-style slow motion: pitch drops with speed.
        flt = f"asetrate={int(48000 * sh['speed'])},aresample=48000"
        x = media.pcm(it['path'], SR, sh['a'], sh['b'] - sh['a'] + .06, 2, f'aresample=48000,{flt}')
    else:
        x = media.pcm(it['path'], SR, sh['a'], sh['b'] - sh['a'] + .06, 2)
    if len(x) < n:
        x = np.vstack([x, np.zeros((n - len(x), 2), np.float32)]) if len(x) else np.zeros((n, 2), np.float32)
    x = x[:n].copy()
    if style.get('mute_slurs', True):
        for a, b in it.get('mute') or []:
            i, j = int((a - .07 - sh['a']) / sh['speed'] * SR), int((b + .07 - sh['a']) / sh['speed'] * SR)
            i, j = max(0, i), min(n, j)
            if j > i:
                bp = sfx.beep((j - i) / SR + .01)[:j - i]
                x[i:i + len(bp)] = bp
    if sh['role'] == 'replay':
        x *= .62
    k = min(int(.010 * SR), n // 3)
    if k:
        r = np.linspace(0, 1, k, dtype=np.float32)[:, None]
        x[:k] *= r
        x[-k:] *= r[::-1]
    return x


def _wav(path, x):
    x = np.clip(x, -1, 1)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype('<i2').tobytes())


def _loudness(path, chain):
    import json, re
    p = subprocess.run([media.tool('ffmpeg'), '-hide_banner', '-nostdin', '-i', str(path), '-af', f'{chain},loudnorm=I=-14:TP=-2:LRA=9:print_format=json', '-f', 'null', '-'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=media.NOWIN)
    m = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', p.stderr.decode('utf-8', 'replace'))
    return json.loads(m[-1]) if m else None


CHAIN = 'highpass=f=55,acompressor=threshold=-21dB:ratio=2.6:attack=6:release=170:makeup=1.5'


# ------------------------------------------------------------------- main
def render(plan, ans, out_path, notify=lambda pct, msg='': None, preview=False):
    """Render an edit plan. Returns dict(duration, thumbs=[(score, frame, t)], timeline)."""
    style = plan['style']
    fps = int(plan.get('fps') or 60)
    shots = build_shots(plan, ans)
    if not shots:
        raise media.MediaError('Nothing to render.')
    pos = 0
    for s in shots:
        s['n'] = max(2, int(round((s['b'] - s['a']) / s['speed'] * fps)))
        s['f0'] = pos
        pos += s['n']
    total = pos
    ranked = plan['kind'] == 'ranking'
    oh = 1280 if preview else int(style.get('out_h', 2560))
    size = (oh * 9 // 16 // 2 * 2, oh)
    k_ = size[0] / W
    for it, an in zip(plan['items'], ans):
        framing.ensure(an, it['path'])
    scenes = [Scene(it, an, style, size) for it, an in zip(plan['items'], ans)]
    # ---- overlay art
    art_tags = [gfx.tag(it['streamer']) for it in plan['items']]
    art_head = (gfx.caption if style.get('energy', 1) < .8 else gfx.headline)(plan['headline']) if plan.get('headline') and style.get('headline', True) else None
    art_cap = gfx.subline(plan['caption']) if style.get('look') and plan.get('caption') and style.get('headline', True) else None
    if art_cap is not None:
        art_head = None                          # one line that stays, not a title that flashes past
    up = lambda im: im if im is None or k_ == 1 else cv2.resize(im, None, fx=k_, fy=k_, interpolation=cv2.INTER_LANCZOS4 if k_ > 1 else cv2.INTER_AREA)
    art_replay, art_cens = gfx.chip('REPLAY'), gfx.chip('CENSORED', (150, 40, 190))
    art_slams = [gfx.number_slam(it['rank']) for it in plan['items']] if ranked else []
    lists = []
    if ranked:
        nums = sorted(it['rank'] for it in plan['items'])
        for i in range(len(plan['items'])):
            shown = {it['rank']: it.get('label') or '' for it in plan['items'][:i + 1]}
            lists.append(gfx.rank_list([(n, shown.get(n, '')) for n in nums], plan['items'][i]['rank']))
    art_tags, art_head, art_replay, art_cens, art_slams, lists = [up(a) for a in art_tags], up(art_head), up(art_replay), up(art_cens), [up(a) for a in art_slams], [up(a) for a in lists]
    art_cap = up(art_cap)
    trans_names = ['whip', 'zoom', 'whip_l', 'flash'] if style.get('energy', 1) >= .8 else [None]
    t_len = max(3, int(.12 * fps))
    for k, s in enumerate(shots):
        prev, nxt = shots[k - 1] if k else None, shots[k + 1] if k + 1 < len(shots) else None
        s['in'] = s['out'] = None
        if prev and prev['i'] != s['i']:
            s['in'] = trans_names[(s['i'] - 1) % len(trans_names)]
        elif prev and prev['role'] == 'teaser':
            s['in'] = 'flash'
        elif prev and s['role'] == 'replay':
            s['in'] = 'zoom'
        if nxt and nxt['i'] != s['i']:
            s['out'] = trans_names[(nxt['i'] - 1) % len(trans_names)]
        elif nxt and nxt['role'] == 'replay':
            s['out'] = 'zoom'
    main_start = next((s['f0'] for s in shots if s['role'] == 'main'), 0)
    head_until = (main_start / fps + 2.2) if main_start else 3.2
    thumbs = []
    with tempfile.TemporaryDirectory(prefix='fc_r_') as td:
        td = Path(td)
        silent = td / 'v.mp4'
        wr = Writer(silent, fps, 'ultrafast' if preview else style.get('preset', 'faster'), 28 if preview else style.get('crf', 18), size)
        done = 0
        try:
            for s in shots:
                it, an, sc = plan['items'][s['i']], ans[s['i']], scenes[s['i']]
                hq = it.get('hq') if it.get('hq') and Path(it['hq']['path']).is_file() else None
                if hq:
                    rd = Reader(hq['path'], an['meta'], s['a'] + hq['shift'], s['b'] + hq['shift'], fps, s['speed'], 'hqdn3d=.8:.8:2:2', tuple(hq.get('size') or ()) or None)
                else:
                    rd = Reader(it['path'], an['meta'], s['a'], s['b'], fps, s['speed'], clean_for(an['meta']))
                try:
                    for j in range(s['n']):
                        if j % 15 == 0:
                            media.check_cancel()
                        src = rd.next()
                        t = s['a'] + (j + .5) * s['speed'] / fps
                        fr = sc.frame(src, t, s.get('jump', 0), s['role'])
                        # ---- thumbnail candidates around beats (clean frames)
                        if s['role'] == 'main' and j % max(1, fps // 5) == 0 and not sc.censored and not preview:
                            _consider(thumbs, fr, s['i'], t, sc, it)
                        gt = (s['f0'] + j) / fps
                        fr = _overlays(fr, gt, j, s, sc, plan, art_head, head_until, art_tags, art_replay, art_cens, art_slams, lists, fps, ranked, art_cap)
                        fr = _transition(fr, j, s, t_len)
                        wr.write(fr if fr.flags['C_CONTIGUOUS'] else np.ascontiguousarray(fr))
                        done += 1
                        if done % 30 == 0:
                            notify(92 * done / total, 'rendering')
                finally:
                    rd.close()
            wr.close()
        except BaseException:
            wr.abort()
            raise
        # ---- audio
        notify(93, 'audio')
        n_s = int(round(total * SR / fps))
        mix = np.zeros((n_s, 2), np.float32)
        for s in shots:
            i0 = int(round(s['f0'] * SR / fps))
            x = shot_audio(s, plan['items'][s['i']], ans[s['i']], round(s['n'] * SR / fps), style)
            e = min(n_s, i0 + len(x))
            mix[i0:e] += x[:e - i0]
        fx = np.zeros_like(mix)
        g = style.get('sfx_gain', 1.0)
        if style.get('sfx', True) and g > 0:
            ref = float(np.sqrt(np.mean(mix ** 2))) or .05
            scale = g * float(np.clip(ref / .11, .30, 1.15))
            for k, s in enumerate(shots):
                at = s['f0'] / fps
                if s['in'] in ('whip', 'whip_l', 'zoom') and s['role'] != 'replay':
                    sfx.mix_into(fx, 'whoosh', at - .17, scale)
                if s['role'] == 'replay':
                    sfx.mix_into(fx, 'whoosh_down', at - .12, scale)
                if s['in'] == 'flash' and k and shots[k - 1]['role'] == 'teaser':
                    sfx.mix_into(fx, 'rewind', at - .06, scale)
                if ranked and s['role'] == 'main' and s.get('seg', 0) == 0:
                    sfx.mix_into(fx, 'pop' if style.get('energy', 1) < .8 else 'ding' if plan['items'][s['i']]['rank'] == 1 else 'impact', at + .02, scale * .8)
                if s['role'] == 'main':
                    po = plan['items'][s['i']]['cut']['payoff']
                    if s['a'] + .4 < po < s['b'] and not ranked and scenes[s['i']].loud > 7 and style.get('energy', 1) >= .8:
                        sfx.mix_into(fx, 'impact', at + (po - s['a']) - .05, scale * .55)
            if art_head is not None and style.get('energy', 1) >= .8:
                sfx.mix_into(fx, 'pop', .04, scale * .7)
        for c in plan.get('voice') or []:
            _mix_voice(mix, c)
        mix = mix + fx
        pk = float(np.abs(mix).max())
        if pk > .985:
            mix *= .985 / pk
        wavp = td / 'a.wav'
        _wav(wavp, mix)
        chain = CHAIN if style.get('compress', True) else 'anull'
        ln = _loudness(wavp, chain)
        if ln and ln.get('input_i') not in ('-inf', None) and float(ln['input_i']) > -60:
            af = (f"{chain},loudnorm=I=-14:TP=-2:LRA=9:measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:measured_LRA={ln['input_lra']}:"
                  f"measured_thresh={ln['input_thresh']}:offset={ln['target_offset']}:linear=true,aresample=48000,alimiter=limit=0.80:attack=2:release=60:level=disabled")
        else:
            af = 'aresample=48000'
        notify(96, 'finishing')
        final = td / 'final.mp4'
        # loudnorm upsamples internally; -ar 48000 here is what keeps clips joinable.
        media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i', silent, '-i', wavp, '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
                   '-af', af, '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2', '-t', f'{total / fps:.4f}', '-movflags', '+faststart',
                   '-metadata', 'comment=FunnyComm edit', final])
        media.place(final, out_path)
    thumbs.sort(key=lambda q: -q['val'])
    notify(100, 'done')
    return {'duration': round(total / fps, 2), 'thumbs': thumbs[:4],
            'timeline': [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items() if k in ('i', 'a', 'b', 'speed', 'role', 'f0', 'n', 'in', 'out')} for s in shots],
            'loudness_in': ln.get('input_i') if ln else None, 'looks': [sc.look for sc in scenes],
            'zooms': sum(len(sc.zooms) for sc in scenes)}


def _consider(pool, fr, i, t, sc, it):
    """Keep the few best cover frames: a clear, expressive face near a big moment."""
    if any(a - .4 <= t <= z + .4 for a, z in it.get('censor') or []) or any(a <= t <= z for a, z in it.get('avoid') or []):
        return
    sm = cv2.resize(fr, (270, 480), interpolation=cv2.INTER_AREA)
    faces = [f for f in vision.detect_faces([sm], thr=.7)[0] if f[2] > .10]
    g = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY)
    sharp = float(np.clip(cv2.Laplacian(g, cv2.CV_32F).var() / 900, 0, 1))
    light = float(np.clip((g.mean() - 22) / 60, 0, 1))
    face = max((f[4] * min(1.0, f[2] / .34) * (1.0 if f[0] > -.02 and f[0] + f[2] < 1.02 else .5) for f in faces), default=0.0)
    beat = max((s_ * math.exp(-((t - b_ - .25) / .9) ** 2) for b_, s_ in sc.beats), default=0)
    val = .46 * face + .24 * beat + .18 * sharp + .12 * light
    for k, q in enumerate(pool):
        if q['i'] == i and abs(q['t'] - t) < 1.3:
            if val > q['val']:
                pool[k] = {'val': val, 'frame': fr.copy(), 'i': i, 't': t, 'seam': sc.seam, 'faces': faces}
            return
    pool.append({'val': val, 'frame': fr.copy(), 'i': i, 't': t, 'seam': sc.seam, 'faces': faces})
    if len(pool) > 9:
        pool.remove(min(pool, key=lambda q: q['val']))


def _mix_voice(mix, c):
    """Your recording or TTS over the edit; original audio ducks under it."""
    try:
        v = media.pcm(c['file'], SR, channels=2)
    except Exception:
        return
    if not len(v):
        return
    v = v / max(.05, float(np.abs(v).max())) * .85
    i = max(0, int(float(c.get('at', .2)) * SR))
    e = min(len(mix), i + len(v))
    if e <= i:
        return
    r = int(.18 * SR)
    env = np.ones(len(mix), np.float32)
    env[i:e] = .26
    a0 = max(0, i - r)
    env[a0:i] = np.linspace(1, .26, i - a0)
    b1 = min(len(mix), e + r)
    env[e:b1] = np.linspace(.26, 1, b1 - e)
    mix *= env[:, None]
    mix[i:e] += v[:e - i]


def _overlays(fr, gt, j, s, sc, plan, head, head_until, tags, replay, cens, slams, lists, fps, ranked, cap=None):
    lt = j / fps
    K = sc.k
    put = lambda im, x, y, al: gfx.blend(fr, im, x * K, y * K, al)
    if cap is not None:
        # the line that stays: low in a framed picture, on the seam of webcam-over-content, lower middle when full screen
        ch_ = cap.shape[0] / K
        if sc.pic and sc.pic[1] < H - 2:
            y = sc.pic[0] - ch_ - 6 if sc.cap_high else sc.pic[0] + sc.pic[1] * .84 - ch_ / 2
        elif sc.seam:
            y = sc.seam - ch_ / 2
        else:
            y = (330 if sc.cap_high else 1235) - ch_ / 2
        put(cap, (W - cap.shape[1] / K) / 2, y, _smooth(gt / .2))
    # attribution, settled after the headline moment
    tg = tags[s['i']]
    calm = plan['style'].get('energy', 1) < .8
    put(tg, 34, min(1640, getattr(sc, 'tag_y', 1470)), (.90 - .52 * _smooth((gt - 3.0) / .8)) if calm else .92)
    if ranked:
        a = _smooth((lt - .45) / .3) if s['role'] == 'main' and s.get('seg', 0) == 0 else 1.0
        put(lists[s['i']], 26, 1448 - lists[s['i']].shape[0] / K, a)
        if not calm and s.get('seg', 0) == 0 and s['role'] == 'main' and lt < .75 and not (s['i'] == 0 and head is not None and gt < .0):
            k = .75 + .25 * min(1, lt / .10) + .06 * math.exp(-lt * 14) * math.sin(lt * 42)
            al = 1.0 if lt < .48 else 1 - _smooth((lt - .48) / .27)
            im = gfx.scaled(slams[s['i']], k)
            put(im, (W - im.shape[1] / K) / 2, (1040 if s['i'] == 0 and head is not None else 740) - im.shape[0] / K / 2, al)
    if sc.censored:
        put(cens, (W - cens.shape[1] / K) / 2, sc.seam + (H - sc.seam) * .38, 1.0)
    if s['role'] == 'replay':
        put(replay, 44, 178, .55 + .45 * (math.sin(lt * 9) > -.2))
    # Above a picture with room over it the line covers nothing, so it may stay until the shot changes.
    room = bool(sc.head_y) and head is not None and sc.head_y >= head.shape[0] / K + 58
    stay = sc.bars and room and calm and not ranked and s['i'] == 0
    if head is not None and s['i'] == 0 and (gt < head_until + .3 or stay) and not getattr(sc, 'head_off', False):
        k = 1.0 if calm or gt > .22 else min(1.06, .74 + 1.9 * gt) if gt < .165 else 1.06 - .06 * (gt - .165) / .055
        al = (1.0 if stay else 1 - _smooth((gt - head_until) / .3)) * (_smooth(gt / .18) if calm else 1)
        im = gfx.scaled(head, k)
        hh = im.shape[0] / K
        y = sc.head_y - 8 - hh / 2 if room else sc.seam if sc.seam else (330 if calm else 300)
        if ranked:
            y = 430 if not sc.seam else sc.seam - 250
        put(im, (W - im.shape[1] / K) / 2, y - im.shape[0] / K / 2, al)
    elif head is not None and gt >= head_until + .3:
        sc.head_off = True
    return fr


def _transition(fr, j, s, n):
    """Motion-matched wipes: accelerate out of A, decelerate into B."""
    if s['in'] and j < n:
        p = j / n
        q = (1 - p) ** 2
        if s['in'] in ('whip', 'whip_l'):
            d = int((1 if s['in'] == 'whip' else -1) * q * fr.shape[1] * .55)
            fr = np.roll(fr, d, axis=1)
            k = int(q * 150) | 1
            if k > 3:
                fr = cv2.blur(fr, (k, 1))
        elif s['in'] == 'zoom':
            fr = _zoom(fr, 1 + .34 * q)
            k = int(q * 18) | 1
            if k > 3:
                fr = cv2.blur(fr, (k, k))
        elif s['in'] == 'flash':
            fr = cv2.addWeighted(fr, 1 - .85 * q, np.full_like(fr, 255), .85 * q, 0)
    left = s['n'] - 1 - j
    if s['out'] and left < n:
        q = (1 - left / n) ** 2
        if s['out'] in ('whip', 'whip_l'):
            d = int((-1 if s['out'] == 'whip' else 1) * q * fr.shape[1] * .55)
            fr = np.roll(fr, d, axis=1)
            k = int(q * 150) | 1
            if k > 3:
                fr = cv2.blur(fr, (k, 1))
        elif s['out'] == 'zoom':
            fr = _zoom(fr, 1 + .30 * q)
            k = int(q * 18) | 1
            if k > 3:
                fr = cv2.blur(fr, (k, k))
    return fr


def _zoom(fr, z):
    h, w = fr.shape[:2]
    M = np.array([[1 / z, 0, w / 2 * (1 - 1 / z)], [0, 1 / z, h / 2 * (1 - 1 / z)]])
    return cv2.warpAffine(fr, M, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)


def clean_for(m):
    """Light noise and block clean-up; starved files get a little more."""
    if not m.get('bytes') or not m.get('duration'):
        return 'hqdn3d=1:1:2.5:2.5'
    bpp = m['bytes'] * 8 / m['duration'] / max(1, m['w'] * m['h'] * min(m['fps'], 60))
    if bpp > .045:
        return 'hqdn3d=.8:.8:2:2'
    if bpp > .022:
        return 'deblock=filter=weak:block=8:alpha=.06:beta=.04,hqdn3d=1.4:1.2:3.5:3.5'
    return 'deblock=filter=weak:block=8:alpha=.09:beta=.05,hqdn3d=2:1.6:4.5:4.5'


DEFAULT_STYLE = {'energy': .45, 'headline': True, 'teaser': False, 'replay': False, 'shake': False, 'reaction_expand': True, 'sfx': True,
                 'sfx_gain': 1.0, 'censor': True, 'blur_chat': True, 'mute_slurs': True, 'compress': True, 'crf': 17, 'preset': 'faster', 'steady': True, 'out_h': 2560, 'zoom': True}
