"""Original-broadcast picture for a clip.

Twitch squeezes clip files hard (often 1-3 Mb/s). For as long as the broadcast is
still on the channel, the very same seconds exist at 6-8 Mb/s. This fetches just
those seconds and lines them up with the clip to the frame, so every edit decision
made on the clip stays valid. Anything doubtful falls back to the clip itself.
"""
from __future__ import annotations
import subprocess, time
from pathlib import Path
import numpy as np
from . import media

PAD = 5.0
_dead = {}                      # broadcasts already known to be gone this session
_urls = {}


def _stream(video_id):
    """(url, headers, w, h, Mb/s) of the best copy of a broadcast, or None."""
    if video_id in _dead:
        return None
    hit = _urls.get(video_id)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    from yt_dlp import YoutubeDL
    try:
        with YoutubeDL({'quiet': True, 'no_warnings': True, 'socket_timeout': 25, 'logger': _Quiet()}) as y:
            info = y.extract_info(f'https://www.twitch.tv/videos/{video_id}', download=False)
    except Exception:
        _dead[video_id] = True
        return None
    fs = [f for f in info.get('formats') or [] if f.get('vcodec') not in (None, 'none') and 700 <= (f.get('height') or 0) <= 1440 and (f.get('tbr') or 0) > 0]
    if not fs:
        _dead[video_id] = True
        return None
    f = max(fs, key=lambda q: (q['height'], q['tbr']))
    au = [q for q in info['formats'] if str(q.get('format_id')).lower().startswith('audio') and q.get('url')]
    out = (f['url'], f.get('http_headers') or {}, f.get('width'), f['height'], f['tbr'] / 1000, au[0]['url'] if au else f['url'])
    _urls[video_id] = (time.time(), out)
    return out


class _Quiet:
    def debug(self, m):
        pass
    info = warning = error = debug


def muted(tw, video_id, a, b):
    """Seconds of [a,b] that Twitch silenced in the broadcast for copyrighted music, or None if unknown."""
    try:
        d = (tw.get('videos', {'id': str(video_id)}).get('data') or [{}])[0]
    except Exception:
        return None
    if not d:
        return None
    return sum(max(0.0, min(b, m['offset'] + m['duration']) - max(a, m['offset'])) for m in d.get('muted_segments') or [])


def _lag(x, y):
    """Where x sits inside y (samples) and how alike they are."""
    n = 1
    while n < len(x) + len(y):
        n *= 2
    c = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(x, n)), n)[:max(1, len(y) - len(x) // 2)]
    k = int(np.argmax(c))
    z = y[k:k + len(x)]
    q = x[:len(z)]
    d = float(np.sqrt((q ** 2).sum() * (z ** 2).sum()))
    return k, float((q * z).sum() / d) if d > 1e-6 else 0.0


def _thumbs(path, t, n, fps=60):
    raw = subprocess.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-ss', f'{max(0, t):.4f}', '-i', str(path), '-an', '-vf',
                          f'setpts=PTS-STARTPTS,fps={fps},scale=96:54:flags=area,format=gray', '-frames:v', str(n), '-f', 'rawvideo', 'pipe:1'],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=media.NOWIN).stdout
    f = np.frombuffer(raw, np.uint8)
    return f[:len(f) // 5184 * 5184].reshape(-1, 54, 96).astype(np.float32)


def align(clip, section, meta):
    """Seconds to add to a clip time to find the same frame in the section, or None."""
    x, y = media.pcm(clip, 8000), media.pcm(section, 8000)
    if len(x) < 8000 or len(y) < len(x) // 2 or np.abs(x).max() < .01:
        return None
    x, y = x - x.mean(), y - y.mean()
    k, r = _lag(x, y)
    if r < .55:
        return None
    base = k / 8000
    fps = min(60, max(24, round(meta['fps'])))
    dur = meta['duration']
    found = []
    for t in np.linspace(.8, max(1.0, dur - 1.2), 6):
        a = _thumbs(clip, t, 1, fps)
        lo = t + base - .5
        if lo < 0 or not len(a):
            continue
        b = _thumbs(section, lo, int(fps * 1.0), fps)
        if len(b) < fps * .6:
            continue
        e = ((b - a[0]) ** 2).mean(axis=(1, 2))
        j = int(np.argmin(e))
        if e[j] > 90 or np.median(e) < e[j] * 1.5 + 1:         # wrong picture, or nothing moves here
            continue
        found.append(j / fps - .5)
    if len(found) < 2:
        return base if not found and r > .9 else (base + found[0] if found and r > .9 else None)
    m = float(np.median(found))
    if np.median(np.abs(np.array(found) - m)) > .05:
        return None
    return base + m


def fetch(src, meta, folder, name='original', notify=lambda m: None):
    """{'path','shift','size','mbps'} for a downloaded clip, or None."""
    vid, off = src.get('video_id'), src.get('vod_offset')
    if not vid or off is None or meta['w'] <= meta['h']:
        return None
    s = _stream(str(vid))
    if not s:
        return None
    url, headers, w, h, mbps, sound = s
    have = meta['bytes'] * 8 / max(.5, meta['duration']) / 1e6 if meta.get('bytes') else 0
    if h < meta['h'] or (h == meta['h'] and mbps < have * 1.15):
        return None                                            # the clip is already as good
    out = Path(folder) / f'{name}.mp4'
    head = ''.join(k + ': ' + v + chr(13) + chr(10) for k, v in headers.items() if k.lower() in ('user-agent', 'referer', 'origin'))

    def grab(link, start, sec, target, *what):
        cmd = [media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', *(['-headers', head] if head else []), '-ss', f'{max(0, start):.2f}', '-i', link, '-t', f'{sec:.2f}', *what, str(target)]
        p = subprocess.run([str(c) for c in cmd], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=media.NOWIN, timeout=240)
        return p.returncode == 0 and Path(target).is_file() and Path(target).stat().st_size > 20_000
    try:
        # Twitch reports roughly where the clip ENDS. Find the exact start by ear first; sound is tiny.
        dur = meta['duration']
        a = max(0.0, float(off) - dur - 16)
        ears = Path(folder) / f'{name}.ts'
        if not grab(sound, a, dur + 34, ears, '-vn', '-c', 'copy', '-f', 'mpegts'):
            return None
        x, y = media.pcm(src['path'], 8000), media.pcm(ears, 8000)
        ears.unlink(missing_ok=True)
        if len(x) < 8000 or len(y) < 8000:
            return None
        k, r = _lag(x - x.mean(), y - y.mean())
        if r < .5:
            return None
        start = a + k / 8000
        if not grab(url, start - PAD, dur + 2 * PAD, out, '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-movflags', '+faststart'):
            return None
        m = media.probe(out)
        shift = align(src['path'], out, meta)
    except (subprocess.TimeoutExpired, media.MediaError, OSError):
        return None
    if shift is None or shift < -.2 or shift + meta['duration'] > m['duration'] + 1.0:
        notify('The original broadcast did not line up; using the clip file.')
        return None
    return {'path': str(out), 'shift': round(shift, 4), 'size': [m['w'], m['h']], 'mbps': round(m['bytes'] * 8 / max(1, m['duration']) / 1e6, 1), 'was': round(have, 1)}
