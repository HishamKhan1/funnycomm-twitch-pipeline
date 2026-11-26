"""One-time, cached analysis of each source clip. Everything downstream reads this."""
from __future__ import annotations
import json, re, time
from pathlib import Path
import cv2
import numpy as np
from . import media, vision, audio, speech, safety, story, ears

SCHEMA = 21
GRID = .1


REAL_LIFE = {'', 'just chatting', 'irl', 'travel & outdoors', 'sports', 'special events', 'music', 'food & drink', 'fitness & health', 'asmr', 'pools, hot tubs, and beaches',
             'the streamer awards', 'nms boxing', 'watch parties', 'events', 'talk shows & podcasts', 'animals, aquariums, and zoos', "i'm only sleeping"}


def is_game(game):
    return (game or '').strip().lower() not in REAL_LIFE


def cache_path(folder, sid):
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', sid)[:110]
    return Path(folder) / f'{safe}.json'


def load(folder, sid):
    p = cache_path(folder, sid)
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        return d if d.get('schema') == SCHEMA else None
    except (OSError, ValueError):
        return None


def _resample(y, rate, n):
    t = (np.arange(n) + .5) * GRID
    x = (np.arange(len(y)) + .5) / rate
    return np.interp(t, x, y) if len(y) else np.zeros(n)


def mouth_activity(lo, rate, tracks):
    """Lower-face motion minus upper-face motion per track: who is talking."""
    n, h, w = lo.shape
    for tr in tracks:
        ts, act = [], []
        a, b = tr['t'][0], tr['t'][-1]
        i0, i1 = max(1, int(a * rate)), min(n - 1, int(b * rate) + 1)
        for i in range(i0, i1):
            t = i / rate
            cx, cy = np.interp(t, tr['t'], tr['cx']), np.interp(t, tr['t'], tr['cy'])
            fw, fh = np.interp(t, tr['t'], tr['w']), np.interp(t, tr['t'], tr['h'])
            x0, x1 = int((cx - fw * .4) * w), int((cx + fw * .4) * w) + 1
            y0, ym, y1 = int((cy - fh * .45) * h), int((cy + fh * .12) * h), int((cy + fh * .55) * h) + 1
            x0, y0 = max(0, x0), max(0, y0)
            if x1 - x0 < 4 or y1 - ym < 3 or ym - y0 < 3:
                continue
            d = np.abs(lo[i, y0:y1, x0:x1].astype(np.int16) - lo[i - 1, y0:y1, x0:x1])
            k = ym - y0
            ts.append(t)
            act.append(float(d[k:].mean()) - .7 * float(d[:k].mean()))
        tr['act_t'] = [round(v, 2) for v in ts]
        tr['act'] = [round(float(v), 2) for v in audio.smooth(act, 7)] if len(act) > 6 else []


def analyse(path, sid, title='', streamer='', folder=None, prior=None, whisper='auto', notify=lambda s: None, force=False, crowd=None, game=''):
    """Full analysis. Returns dict; cached on disk when folder is given."""
    path = Path(path)
    st = path.stat()
    if folder and not force:
        d = load(folder, sid)
        if d and d.get('bytes') == st.st_size:
            # Older results judged keyed-webcam gameplay as real-world footage.
            if not (is_game(game) and not d['layout']['cam'] and not d.get('as_game')):
                return d
            redo = d
        else:
            redo = None
    else:
        redo = None
    if redo is not None:
        meta = redo['meta']
        hi, hr, lo, lr = media.sample_dual(path, meta, hi_fps=4.0)
        nude = safety.scan_frames(hi, hr, None)
        redo['safety'] = safety.assess(title, meta['duration'], nude, redo['speech']['words'], redo['speech']['ok'], redo['audio']['music'], meta['audio'], irl=False)
        redo['as_game'] = True
        media.write_json(cache_path(folder, sid), redo)
        return redo
    t0 = time.time()
    meta = media.probe(path)
    media.check_cancel()
    hi, hr, lo, lr = media.sample_dual(path, meta, hi_fps=4.0)
    dur = meta['duration']
    faces = vision.detect_faces(hi)
    portrait = meta['w'] <= meta['h']
    cam = chat = caps = green = None
    bars = [0.0] * 4
    maps = vision.overlay_maps(hi, lo)
    if not portrait:
        cam = vision.find_webcam(hi, lo, faces, maps, prior)
        if not cam:
            # Very small webcams need a second, sharper look.
            idx = np.linspace(0, len(hi) - 1, min(24, len(hi))).astype(int)
            f2 = vision.detect_faces([hi[i] for i in idx], thr=.55, upscale=1.75)
            cam = vision.find_webcam(hi[idx], lo, f2, maps, prior) if any(f2) else None
            if cam:
                faces2 = vision.detect_faces(hi, thr=.5, upscale=1.4)
                faces = faces2
        chat = vision.find_chat(maps, cam)
        bars = vision.find_bars(hi)
        caps = vision.find_captions(hi)
        if cam and cam['kind'] == 'box':
            green = vision.green_screen(hi, cam['rect'])
    media.check_cancel()
    cuts = vision.shot_cuts(lo, lr)
    cuts_s = [round(c / lr, 2) for c in cuts]
    glob, camc, chatc, gx = vision.curves(lo, lr, cam, chat, caps)
    ix, istr = vision.interest_track(gx, lr, faces, hr, cam, len(lo))
    notify('listening')
    aud = audio.analyse(path, meta['audio'])
    media.check_cancel()
    sp = speech.transcribe(path, whisper) if meta['audio'] and not aud.get('silent') else {'ok': True, 'model': None, 'words': [], 'lines': [], 'error': None}
    media.check_cancel()
    x16 = media.pcm(path, 16000) if meta['audio'] and not aud.get('silent') else np.zeros(0, np.float32)
    tags = ears.tag(x16) if len(x16) else None
    if tags:
        aud['music'] = ears.music_report(tags) or aud['music']
    chat_log = ears.chat_events(path, meta, chat[0]) if chat else None
    notify('safety check')
    nude = safety.scan_frames(hi, hr, cam)
    report = safety.assess(title, dur, nude, sp['words'], sp['ok'], aud['music'], meta['audio'], irl=cam is None and not is_game(game))
    n = max(5, int(dur / GRID))
    ex = _resample(aud['excite'], 1 / aud['hop'], n) if aud['excite'] else np.zeros(n)
    if tags:
        tt, gt = np.arange(len(tags['laugh'])) * tags['hop'] + tags['offset'], (np.arange(n) + .5) * GRID
        lg = np.interp(gt, tt, tags['laugh'])
        sc_ = np.interp(gt, tt, tags['scream'])
        cr_ = np.interp(gt, tt, np.maximum(tags['crowd'], 0))
        cry = np.interp(gt, tt, tags['cry'])
    else:
        lg = _resample(aud['laugh'], 1 / aud['hop'], n) if aud['laugh'] else np.zeros(n)
        sc_, cr_, cry = np.zeros(n), np.zeros(n), np.zeros(n)
    chat_lol, chat_wow = ears.chat_curves(chat_log, n, GRID)
    tracks = []
    face_cam = []
    if cam:
        x, y, a, b = cam['rect']
        for i, rows in enumerate(faces):
            inside = [f for f in rows if x - .01 <= f[0] + f[2] / 2 <= x + a + .01 and y - .01 <= f[1] + f[3] / 2 <= y + b + .01]
            if inside:
                f = max(inside, key=lambda r: r[2] * r[4])
                face_cam.append([round((i + .5) / hr, 2)] + [round(v, 4) for v in f[:4]] + [_open(f)])
        # A second stable person inside the webcam changes how it should be framed.
        cnt = [sum(1 for f in rows if x <= f[0] + f[2] / 2 <= x + a and y <= f[1] + f[3] / 2 <= y + b) for rows in faces]
        cam['people'] = 2 if cnt and np.mean(np.array(cnt) >= 2) > .4 else 1
    else:
        tr = vision.subject_tracks(faces, hr, cuts_s, dur, hi)
        mouth_activity(lo, lr, tr)
        for q in tr:
            if len(q['t']) >= 2:
                tracks.append({'t': [round(v, 2) for v in q['t']], 'cx': [round(v, 4) for v in q['cx']], 'cy': [round(v, 4) for v in q['cy']],
                               'w': [round(v, 4) for v in q['w']], 'h': [round(v, 4) for v in q['h']],
                               'shot': [round(v, 2) for v in q['shot']], 'act_t': q.get('act_t', []), 'act': q.get('act', [])})
    out = {
        'schema': SCHEMA, 'id': sid, 'path': str(path), 'bytes': st.st_size, 'title': title, 'streamer': streamer, 'meta': meta,
        'layout': {'mode': 'portrait' if portrait else 'split' if cam else ('irl' if tracks else 'scene'),
                   'cam': cam, 'green': green, 'chat': chat or [], 'captions': caps, 'bars': bars, 'cuts': cuts_s},
        'face_cam': face_cam, 'tracks': tracks,
        'grid': GRID, 'curves': {
            'audio': _r(ex), 'laugh': _r(lg), 'scream': _r(sc_), 'crowd': _r(cr_), 'cry': _r(cry), 'chat_lol': _r(chat_lol, 2), 'chat_wow': _r(chat_wow, 2), 'motion': _r(_resample(glob, lr, n)), 'cam': _r(_resample(camc, lr, n)),
            'chat': _r(_resample(chatc, lr, n), 4), 'ix': _r(_resample(ix, lr, n)), 'is': _r(_resample(istr, lr, n))},
        'audio': {k: aud.get(k) for k in ('floor_db', 'mid_db', 'peak_gap_db', 'music', 'silent')},
        'speech': sp, 'safety': report, 'chat_text': [t for e in (chat_log or []) for t in e['text']][:80], 'as_game': is_game(game), 'tagged': bool(tags), 'crowd': crowd, 'faces': {'f': nude['f'], 'm': nude['m']} if isinstance(nude, dict) else None,
    }
    out['moments'] = story.moments(out)
    out['took'] = round(time.time() - t0, 1)
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        media.write_json(cache_path(folder, sid), out)
        storyboard(hi, out, cache_path(folder, sid).with_suffix('.jpg'))
    return out


def _open(f):
    """Rough mouth-width vs eye-distance expression cue from 5 landmarks."""
    try:
        rx, ry, lx, ly, nx, ny, mrx, mry, mlx, mly = f[5:15]
        eye = abs(rx - lx) + 1e-4
        drop = ((mry + mly) / 2 - ny) / (abs(ny - (ry + ly) / 2) + 1e-4)
        return round(float(np.clip(drop, 0, 3)), 2) if eye > .001 else 0.0
    except Exception:
        return 0.0


def _r(a, d=3):
    return [round(float(v), d) for v in a]


def storyboard(hi, an, target):
    """8-frame preview with detected regions, shown in the app."""
    idx = np.linspace(0, len(hi) - 1, 8).astype(int)
    tiles = []
    for i in idx:
        im = hi[i].copy()
        H, W = im.shape[:2]
        c = an['layout']['cam']
        if c:
            x, y, a, b = c['rect']
            cv2.rectangle(im, (int(x * W), int(y * H)), (int((x + a) * W), int((y + b) * H)), (90, 220, 60), 4)
        tiles.append(cv2.resize(im, (320, int(320 * H / W)), interpolation=cv2.INTER_AREA))
    ok, buf = cv2.imencode('.jpg', np.vstack([np.hstack(tiles[:4]), np.hstack(tiles[4:])]), [cv2.IMWRITE_JPEG_QUALITY, 84])
    if ok:
        Path(target).write_bytes(buf.tobytes())
