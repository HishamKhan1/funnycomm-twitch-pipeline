"""Original sound design, synthesised in code. No samples, nothing to claim."""
from __future__ import annotations
import numpy as np

SR = 48000
_cache = {}


def _noise(n, seed):
    return np.random.default_rng(seed).standard_normal(n)


def _bandsweep(n, f0, f1, q, seed):
    """Noise through a swept resonator."""
    x = _noise(n, seed)
    f = np.geomspace(f0, f1, n)
    y = np.zeros(n)
    a1 = a2 = 0.0
    for i in range(n):
        w = 2 * np.pi * f[i] / SR
        r = np.exp(-w / (2 * q))
        v = x[i] * (1 - r) + 2 * r * np.cos(w) * a1 - r * r * a2
        a2, a1 = a1, v
        y[i] = v
    return y / (np.abs(y).max() + 1e-9)


def _st(x, width=0.0):
    if width:
        d = int(width * SR)
        r = np.r_[np.zeros(d), x[:-d]] if d else x
        return np.stack([x, r], 1).astype(np.float32)
    return np.stack([x, x], 1).astype(np.float32)


def whoosh(dur=.34, up=True):
    n = int(dur * SR)
    y = _bandsweep(n, 350 if up else 2600, 2800 if up else 320, 1.6, 7)
    env = np.sin(np.linspace(0, np.pi, n)) ** 1.6
    return _st(y * env * .55, .006)


def impact(dur=.55):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 44 + 95 * np.exp(-t * 26)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 8.5)
    click = _noise(n, 3) * np.exp(-t * 190) * .30
    return _st(np.tanh((body + click) * 1.4) * .7)


def pop(dur=.13):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 420 + 620 * np.exp(-t * 42)
    return _st(np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 34) * .6)


def riser(dur=.9):
    n = int(dur * SR)
    t = np.arange(n) / SR
    y = _bandsweep(n, 260, 3600, 2.2, 11) * .5
    tone = np.sin(2 * np.pi * np.cumsum(np.geomspace(150, 620, n)) / SR) * .28
    return _st((y + tone) * (t / dur) ** 2.0 * .6, .004)


def rewind(dur=.42):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 900 + 700 * np.sin(2 * np.pi * 13 * t) - 900 * t
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * .22 + _bandsweep(n, 3000, 500, 1.4, 5) * .4
    return _st(y * np.sin(np.linspace(0, np.pi, n)) ** .8, .003)


def ding(dur=.7):
    n = int(dur * SR)
    t = np.arange(n) / SR
    y = sum(a * np.sin(2 * np.pi * f * t) * np.exp(-t * d) for f, a, d in ((1318.5, .5, 7), (1975.5, .28, 9), (2637, .16, 12)))
    return _st(y * .55)


def beep(dur):
    n = max(1, int(dur * SR))
    t = np.arange(n) / SR
    y = np.sin(2 * np.pi * 1000 * t) * .16
    k = min(n // 2, 240)
    if k:
        y[:k] *= np.linspace(0, 1, k)
        y[-k:] *= np.linspace(1, 0, k)
    return _st(y)


def get(name):
    if name not in _cache:
        _cache[name] = {'whoosh': whoosh, 'whoosh_down': lambda: whoosh(up=False), 'impact': impact, 'pop': pop, 'riser': riser,
                        'rewind': rewind, 'ding': ding}[name]()
    return _cache[name]


LEVEL = {'whoosh': .40, 'whoosh_down': .36, 'impact': .50, 'pop': .42, 'riser': .26, 'rewind': .40, 'ding': .38}


def mix_into(track, name, at, gain=1.0):
    s = get(name) * (LEVEL.get(name, .4) * gain)
    i = int(round(at * SR))
    if i < 0:
        s, i = s[-i:], 0
    e = min(len(track), i + len(s))
    if e > i:
        track[i:e] += s[:e - i]
