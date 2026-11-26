"""Automatic checks on a finished Short. Catches technical faults, not taste."""
from __future__ import annotations
import re, subprocess
import cv2
import numpy as np
from . import media, vision


def _loud(path):
    p = subprocess.run([media.tool('ffmpeg'), '-hide_banner', '-nostdin', '-i', str(path), '-af', 'ebur128=peak=true', '-f', 'null', '-'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=media.NOWIN)
    t = p.stderr.decode('utf-8', 'replace')
    i = re.findall(r'I:\s+(-?[\d.]+) LUFS', t)
    k = re.findall(r'Peak:\s+(-?[\d.]+) dBFS', t)
    return (float(i[-1]) if i else None), (float(k[-1]) if k else None)


def check(path, expected=None, split=False, framed=False):
    m = media.probe(path)
    issues = []
    if (m['w'], m['h']) not in ((1080, 1920), (1440, 2560)):
        issues.append(f"Wrong size {m['w']}x{m['h']}")
    if not 3 <= m['duration'] <= 179:
        issues.append(f"Length {m['duration']:.1f}s is outside Shorts limits")
    if expected and abs(m['duration'] - expected) > .25:
        issues.append(f"Length {m['duration']:.2f}s differs from the edit plan ({expected:.2f}s)")
    if not m['audio']:
        issues.append('No audio track')
    elif m['sample_rate'] != 48000:
        issues.append(f"Audio is {m['sample_rate']} Hz, expected 48000")
    w, h = 270, 480
    rate = min(4.0, 160 / max(m['duration'], 1))
    raw = media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', path, '-an', '-vf', f'fps={rate},scale={w}:{h}:flags=area,format=bgr24', '-f', 'rawvideo', 'pipe:1']).stdout
    f = np.frombuffer(raw, np.uint8)
    f = f[:len(f) // (w * h * 3) * (w * h * 3)].reshape(-1, h, w, 3)
    if not len(f):
        return {'ok': False, 'issues': issues + ['No frames decoded'], 'metrics': {}}
    g = f.mean(axis=3)
    lum = g.mean(axis=(1, 2))
    black = float((lum < 9).mean())
    if black > .12:
        issues.append(f'{black * 100:.0f}% of frames are almost black')
    d = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2)) if len(g) > 1 else np.array([1.0])
    still, run = 0, 0
    for v in d:
        run = run + 1 if v < .12 else 0
        still = max(still, run)
    if still / rate > 2.2:
        issues.append(f'Picture is frozen for {still / rate:.1f}s')
    # Bars: rows near the top/bottom that are flat in every frame.
    rs = g.std(axis=2)
    flat = (np.percentile(rs, 90, axis=0) < 2.2)
    top = int(np.argmax(~flat)) if (~flat).any() else h
    bot = int(np.argmax(~flat[::-1])) if (~flat).any() else h
    if top + bot > .10 * h and not framed:
        issues.append(f'Empty bars cover {(top + bot) / h * 100:.0f}% of the frame')
    ims = [cv2.resize(x, (540, 960)) for x in f[::max(1, len(f) // 24)]]
    faces = vision.detect_faces(ims, thr=.6)
    share = float(np.mean([any(q[2] > .07 for q in r) for r in faces]))
    cut = float(np.mean([any(q[2] > .10 and (q[0] < -.02 or q[0] + q[2] > 1.02 or q[1] < -.015) for q in r) for r in faces]))
    if split and share < .25:
        issues.append('Streamer face is rarely visible in the facecam panel')
    if cut > .30:
        issues.append('A face is cut off by the edge in many frames')
    i, pk = _loud(path) if m['audio'] else (None, None)
    if i is not None and not -17.5 <= i <= -11.5:
        issues.append(f'Loudness is {i:.1f} LUFS (target -14)')
    if pk is not None and pk > -.1:
        issues.append(f'Audio peaks at {pk:.1f} dBFS')
    return {'ok': not issues, 'issues': issues, 'metrics': {
        'seconds': round(m['duration'], 2), 'fps': round(m['fps'], 2), 'mb': round(m['bytes'] / 1e6, 1), 'black': round(black, 3), 'frozen_s': round(still / rate, 1),
        'bars': round((top + bot) / h, 3), 'face_share': round(share, 2), 'face_cut': round(cut, 2), 'lufs': i, 'peak_db': pk, 'sample_rate': m['sample_rate']}}


def contact(path, target, n=10):
    m = media.probe(path)
    media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i', path, '-vf', f"fps={n / max(m['duration'], .5):.5f},scale=216:-2,tile={n // 2}x2", '-frames:v', '1', target])
    return target
