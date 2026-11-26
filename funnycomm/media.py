"""FFmpeg helpers: probing, sampling, PCM decode, atomic file placement."""
from __future__ import annotations
import hashlib, json, os, shutil, subprocess, tempfile, threading
from fractions import Fraction
from pathlib import Path
import numpy as np

APP = Path(__file__).resolve().parent.parent
NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SR = 48000


class MediaError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    """Raised when the user presses Stop; never treated as a failed clip."""


_cancel = threading.Event()


def request_cancel():
    _cancel.set()


def clear_cancel():
    _cancel.clear()


def check_cancel():
    if _cancel.is_set():
        raise Cancelled('Stopped by user')


_found: dict[str, str] = {}


def tool(name: str) -> str:
    """Locate ffmpeg/ffprobe: bundled copies first, then PATH, then WinGet."""
    if name in _found:
        return _found[name]
    ext = '.exe' if os.name == 'nt' else ''
    spots = [APP / 'ffmpeg' / 'bin' / (name + ext), APP.parent / 'ffmpeg' / 'bin' / (name + ext), APP / (name + ext)]
    hit = next((str(p) for p in spots if p.is_file()), None) or shutil.which(name)
    if not hit and os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'WinGet'
        links = base / 'Links' / (name + ext)
        if links.is_file():
            hit = str(links)
        elif (base / 'Packages').is_dir():
            hit = next((str(p) for p in (base / 'Packages').glob(f'Gyan.FFmpeg*/**/bin/{name}{ext}')), None)
    if not hit:
        raise MediaError(f'{name} was not found. Install FFmpeg (winget install Gyan.FFmpeg) and restart the app.')
    _found[name] = hit
    return hit


def run(args, **kw):
    p = subprocess.run([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                       creationflags=NOWIN, **kw)
    if p.returncode:
        raise MediaError((p.stderr or b'').decode('utf-8', 'replace').strip()[-900:] or f'command failed ({p.returncode})')
    return p


def probe(path) -> dict:
    d = json.loads(run([tool('ffprobe'), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path]).stdout)
    v = next((s for s in d.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not v:
        raise MediaError(f'No video stream in {Path(path).name}')
    a = next((s for s in d['streams'] if s.get('codec_type') == 'audio'), None)

    def rate(key):
        try:
            return float(Fraction(v.get(key, '0/1')))
        except (ValueError, ZeroDivisionError):
            return 0.0
    fps = rate('avg_frame_rate') or rate('r_frame_rate') or 30.0
    rot = next((s.get('rotation', 0) for s in v.get('side_data_list', []) if 'rotation' in s), 0)
    w, h = int(v['width']), int(v['height'])
    if abs(int(rot)) % 180 == 90:
        w, h = h, w
    dur = float(d.get('format', {}).get('duration') or v.get('duration') or 0)
    return {'w': w, 'h': h, 'fps': fps, 'duration': dur, 'audio': a is not None,
            'sample_rate': int(a['sample_rate']) if a and a.get('sample_rate') else 0,
            'vcodec': v.get('codec_name', ''), 'bytes': int(d.get('format', {}).get('size') or 0)}


def verify_playable(path, min_seconds=.5) -> dict:
    """Fully decode once. Catches truncated downloads and bad drive reads."""
    m = probe(path)
    if m['duration'] < min_seconds:
        raise MediaError('Video is too short to use.')
    p = subprocess.run([tool('ffmpeg'), '-v', 'error', '-nostdin', '-xerror', '-i', str(path), '-f', 'null', '-'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NOWIN)
    if p.returncode:
        raise MediaError('File does not decode cleanly: ' + p.stderr.decode('utf-8', 'replace')[-240:])
    return m


def pcm(path, sr=16000, start=None, dur=None, channels=1, filters=None) -> np.ndarray:
    """Float32 PCM. Shape (n,) for mono or (n, channels)."""
    args = [tool('ffmpeg'), '-v', 'error', '-nostdin']
    if start is not None:
        args += ['-ss', f'{start:.4f}']
    if dur is not None:
        args += ['-t', f'{dur:.4f}']
    args += ['-i', str(path), '-vn', '-map', '0:a:0?']
    if filters:
        args += ['-af', filters]
    args += ['-ac', str(channels), '-ar', str(sr), '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1']
    raw = subprocess.run([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN).stdout
    x = np.frombuffer(raw, np.float32)
    if channels > 1:
        x = x[:len(x) // channels * channels].reshape(-1, channels)
    return np.nan_to_num(x.copy())


def sample_dual(path, meta, hi_fps=3.0, hi_w=960, lo_fps=12.0, lo_w=320):
    """One decode pass -> (hi BGR frames, hi rate, lo gray frames, lo rate)."""
    dur = max(meta['duration'], .1)
    hi_fps = min(hi_fps, 180 / dur)
    hi_fps = max(hi_fps, 8 / dur)
    lo_fps = min(lo_fps, 900 / dur)
    hi_h = int(round(hi_w * meta['h'] / meta['w'] / 2) * 2)
    lo_h = int(round(lo_w * meta['h'] / meta['w'] / 2) * 2)
    with tempfile.TemporaryDirectory(prefix='fc_s_') as td:
        lo_path = Path(td) / 'lo.raw'
        graph = (f'[0:v]split=2[a][b];[a]fps={hi_fps:.6f},scale={hi_w}:{hi_h}:flags=area,setsar=1,format=bgr24[av];'
                 f'[b]fps={lo_fps:.6f},scale={lo_w}:{lo_h}:flags=area,setsar=1,format=gray[bv]')
        out = run([tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', path, '-an', '-filter_complex', graph,
                   '-map', '[av]', '-f', 'rawvideo', 'pipe:1', '-map', '[bv]', '-f', 'rawvideo', lo_path]).stdout
        hi = np.frombuffer(out, np.uint8)
        n = len(hi) // (hi_w * hi_h * 3)
        hi = hi[:n * hi_w * hi_h * 3].reshape(n, hi_h, hi_w, 3)
        lo = np.fromfile(lo_path, np.uint8)
        m = len(lo) // (lo_w * lo_h)
        lo = lo[:m * lo_w * lo_h].reshape(m, lo_h, lo_w)
    if n < 2 or m < 4:
        raise MediaError('Could not decode enough frames to analyse this clip.')
    return hi, hi_fps, lo, lo_fps


def grab(path, t, width=None):
    """Single BGR frame near t seconds."""
    import cv2
    vf = ['-vf', f'scale={width}:-2'] if width else []
    raw = run([tool('ffmpeg'), '-v', 'error', '-nostdin', '-ss', f'{max(0, t):.3f}', '-i', path, '-frames:v', '1', *vf,
               '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1']).stdout
    im = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
    if im is None:
        raise MediaError('Could not read a frame.')
    return im


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(2 << 20), b''):
            h.update(block)
    return h.hexdigest()


def place(src, dest):
    """Copy to the final folder, verify by hash, then rename atomically.

    The data drive is a USB enclosure that has logged controller resets, so a
    finished render is only accepted once its bytes read back correctly.
    """
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.part')
    want = sha256(src)
    last = None
    for _ in range(3):
        try:
            shutil.copyfile(src, tmp)
            if sha256(tmp) == want:
                os.replace(tmp, dest)
                return dest
        except OSError as e:
            last = e
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise MediaError(f'{dest.name} could not be saved correctly to the data drive' + (f': {last}' if last else
                     ' (contents did not verify). Check the external drive connection.'))


def write_text(path, text):
    """Atomic UTF-8 text write."""
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def write_json(path, obj):
    write_text(path, json.dumps(obj, indent=1, ensure_ascii=False))
