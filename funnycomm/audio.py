"""Audio understanding: loudness/excitement curves, laughter, silence, music, fingerprints."""
from __future__ import annotations
import numpy as np
from . import media

HOP = .05          # seconds per curve sample
FP_SR = 5512
FP_HOP = 128 / FP_SR


def _stft(x, n, hop):
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    frames = 1 + (len(x) - n) // hop
    idx = np.lib.stride_tricks.sliding_window_view(x, n)[::hop][:frames]
    return np.abs(np.fft.rfft(idx * np.hanning(n).astype(np.float32), axis=1)).astype(np.float32)


def smooth(a, k):
    k = max(1, int(k))
    if k == 1 or len(a) < 3:
        return np.asarray(a, float)
    ker = np.hanning(k + 2)[1:-1]
    ker /= ker.sum()
    pad = k // 2
    return np.convolve(np.pad(a, (pad, k - 1 - pad), mode='edge'), ker, mode='valid')


def norm01(a, lo=50, hi=98):
    a = np.asarray(a, float)
    if not len(a):
        return a
    p, q = np.percentile(a, lo), np.percentile(a, hi)
    return np.clip((a - p) / max(1e-6, q - p), 0, 1.25)


def analyse(path, has_audio=True):
    """Curves on a 50 ms grid plus summary measures."""
    if not has_audio:
        return {'hop': HOP, 'db': [], 'excite': [], 'laugh': [], 'music': {'score': 0.0, 'spans': []}, 'silent': True}
    x = media.pcm(path, 16000)
    if len(x) < 8000:
        return {'hop': HOP, 'db': [], 'excite': [], 'laugh': [], 'music': {'score': 0.0, 'spans': []}, 'silent': True}
    hop = int(16000 * HOP)
    S = _stft(x, 1024, hop)
    f = np.fft.rfftfreq(1024, 1 / 16000)
    P = S ** 2
    total = P.sum(axis=1) + 1e-10
    db = 10 * np.log10(total / 1024 + 1e-10)
    cent = (P * f).sum(axis=1) / total
    hi = P[:, (f > 1500) & (f < 6000)].sum(axis=1) / total
    flux = np.r_[0, np.clip(np.diff(np.log1p(S * 10), axis=0), 0, None).mean(axis=1)]
    floor = np.percentile(db, 10)
    voiced_db = db[db > floor + 6] if (db > floor + 6).any() else db
    mid = np.percentile(voiced_db, 50)
    loud = np.clip((db - mid) / 10.0, -.6, 1.4)
    bright = np.clip((cent - np.median(cent)) / 1400, -.5, 1)
    excite = np.clip(loud + .45 * bright + .25 * norm01(flux), 0, 2)
    # Laughter and hype chants pulse at roughly 3.5-7 Hz in the speech band.
    fine = 160
    env = np.sqrt(np.convolve(x ** 2, np.ones(400) / 400, mode='same'))[::fine]
    e = env - smooth(env, 60)
    win, laugh = 120, np.zeros(len(db))
    ff = np.fft.rfftfreq(win, fine / 16000)
    band = (ff > 3.4) & (ff < 7.2)
    for i in range(0, max(0, len(e) - win), 20):
        sp = np.abs(np.fft.rfft(e[i:i + win] * np.hanning(win))) ** 2
        r = sp[band].sum() / (sp[1:].sum() + 1e-9)
        pk = sp[band].max() / (sp[band].mean() + 1e-9)
        j = int((i + win / 2) * fine / 16000 / HOP)
        if j < len(laugh):
            laugh[max(0, j - 2):j + 3] = np.maximum(laugh[max(0, j - 2):j + 3], np.clip((r - .34) * 2.2, 0, 1) * min(1, pk / 3.2))
    laugh *= db > mid - 8
    return {'hop': HOP, 'db': np.round(db, 1).tolist(), 'excite': np.round(excite, 3).tolist(),
            'laugh': np.round(laugh, 3).tolist(), 'floor_db': round(float(floor), 1), 'mid_db': round(float(mid), 1),
            'peak_gap_db': round(float(np.percentile(db, 99) - mid), 1), 'music': music(x), 'silent': bool(np.max(db) < -60)}


def music(x):
    """Conservative background-music indicator (steady tones + regular beat).

    It can miss music under shouting and can fire on game ambience. It exists to
    tell the reviewer where to listen, not to declare anything clear.
    """
    n, hop = 4096, 1024
    S = _stft(x, n, hop)
    if len(S) < 24:
        return {'score': 0.0, 'spans': []}
    f = np.fft.rfftfreq(n, 1 / 16000)
    sel = (f > 90) & (f < 3200)
    M = np.log1p(20 * S[:, sel])
    local = np.apply_along_axis(lambda r: np.convolve(r, np.ones(25) / 25, mode='same'), 1, M)
    peak = (M > np.roll(M, 1, axis=1)) & (M >= np.roll(M, -1, axis=1)) & (M > local * 1.25 + .05)
    # A tone counts when the same bin (+-1) peaks for >= 10 consecutive frames (~0.64 s).
    p = peak | np.roll(peak, 1, axis=1) & False
    run = np.zeros(p.shape[1], np.int32)
    held = np.zeros_like(M, dtype=bool)
    for i in range(len(p)):
        prev = np.maximum(run, np.maximum(np.r_[run[1:], 0], np.r_[0, run[:-1]]))
        run = np.where(p[i], prev + 1, 0)
        held[i] = run >= 10
    share = (np.expm1(M) * held).sum(axis=1) / (np.expm1(M).sum(axis=1) + 1e-6)
    share = smooth(share, 12)
    onset = np.clip(np.diff(M, axis=0), 0, None).mean(axis=1)
    onset = onset - smooth(onset, 24)
    fr = 16000 / hop
    beat = np.zeros(len(share))
    w = int(6 * fr)
    for i in range(0, max(1, len(onset) - w), int(1.5 * fr)):
        seg = onset[i:i + w]
        if len(seg) < w * .8:
            continue
        ac = np.correlate(seg, seg, 'full')[len(seg) - 1:]
        ac = ac / (ac[0] + 1e-9)
        lo, hi = int(fr * 60 / 175), int(fr * 60 / 65)
        if hi < len(ac):
            beat[i:i + w] = np.maximum(beat[i:i + w], ac[lo:hi].max())
    score = np.clip(share * 5.5, 0, 1) * .65 + np.clip((beat - .12) * 2.6, 0, 1) * .35
    on = score > .5
    spans, start = [], None
    for i, v in enumerate(list(on) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if (i - start) / fr >= 2.0:
                spans.append([round(start / fr, 1), round(i / fr, 1)])
            start = None
    return {'score': round(float(np.percentile(score, 75)), 2), 'spans': spans[:8]}


# ------------------------------------------------------------ fingerprints
def fingerprint(path):
    """32-bit spectral-difference codes every ~23 ms. Survives re-encoding and resizing."""
    x = media.pcm(path, FP_SR)
    if len(x) < FP_SR:
        return np.zeros(0, np.uint32)
    S = _stft(x, 2048, 128) ** 2
    f = np.fft.rfftfreq(2048, 1 / FP_SR)
    edges = np.geomspace(300, 2000, 34)
    E = np.stack([S[:, (f >= a) & (f < b)].sum(axis=1) if ((f >= a) & (f < b)).any() else S[:, int(np.argmin(abs(f - a)))]
                  for a, b in zip(edges, edges[1:])], axis=1)
    d = np.diff(E, axis=1)
    bits = (np.diff(d, axis=0) > 0)
    return (bits * (1 << np.arange(32, dtype=np.uint64))).sum(axis=1).astype(np.uint32)


def match(a, b, min_seconds=3.5):
    """Best alignment of two fingerprints -> dict(ber, seconds, offset) or None.

    offset is where b starts inside a, in seconds (negative when b starts first).
    """
    if len(a) < 40 or len(b) < 40:
        return None
    need = max(int(min_seconds / FP_HOP), int(.35 * min(len(a), len(b))))
    need = min(need, int(.9 * min(len(a), len(b))))
    step = 2
    best = (1.0, 0, 0)
    for lag in range(-(len(b) - need), len(a) - need + 1, step):
        i0, j0 = max(0, lag), max(0, -lag)
        n = min(len(a) - i0, len(b) - j0)
        sub = slice(0, n, 4)
        ber = float(np.bitwise_count(a[i0:i0 + n][sub] ^ b[j0:j0 + n][sub]).mean()) / 32
        if ber < best[0]:
            best = (ber, n, lag)
    if best[0] > .40:
        return None
    ber0, n0, lag0 = best
    for lag in range(lag0 - 3, lag0 + 4):
        i0, j0 = max(0, lag), max(0, -lag)
        n = min(len(a) - i0, len(b) - j0)
        if n < need:
            continue
        ber = float(np.bitwise_count(a[i0:i0 + n] ^ b[j0:j0 + n]).mean()) / 32
        if ber < best[0]:
            best = (ber, n, lag)
    ber, n, lag = best
    return {'ber': round(ber, 3), 'seconds': round(n * FP_HOP, 1), 'offset': round(lag * FP_HOP, 2)}


SAME_BER = .18   # measured on 114 files: same moment <= 0.075, different streamers on the same cutscene >= 0.26
