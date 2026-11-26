"""Laughter / scream / music tagging and stream-chat reading. Offline."""
from __future__ import annotations
import csv, difflib, re, subprocess
from pathlib import Path
import numpy as np
from . import media

AUDIO_DIR = Path(__file__).resolve().parent.parent / 'models' / 'audio'
_t, _o = {}, {}
LAUGH = {'Laughter', 'Giggle', 'Snicker', 'Belly laugh', 'Chuckle, chortle', 'Baby laughter'}
SCREAM = {'Screaming', 'Shout', 'Yell', 'Bellow', 'Whoop', 'Children shouting', 'Battle cry'}
CROWD = {'Cheering', 'Applause', 'Crowd', 'Clapping', 'Hubbub, speech noise, speech babble'}
CRY = {'Crying, sobbing', 'Whimper', 'Wail, moan'}
MUSIC = {'Music', 'Singing', 'Musical instrument', 'Pop music', 'Hip hop music', 'Rapping', 'Song', 'Electronic music', 'Rhythm and blues', 'Background music', 'Choir', 'Vocal music', 'Theme music', 'Dance music'}


def tagger_ready():
    try:
        import sherpa_onnx  # noqa: F401
        return (AUDIO_DIR / 'ced_mini.int8.onnx').is_file()
    except Exception:
        return False


def ocr_ready():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _tagger():
    if 't' not in _t:
        import sherpa_onnx
        cfg = sherpa_onnx.AudioTaggingConfig(
            model=sherpa_onnx.AudioTaggingModelConfig(ced=str(AUDIO_DIR / 'ced_mini.int8.onnx'), num_threads=4, provider='cpu'),
            labels=str(AUDIO_DIR / 'class_labels_indices.csv'), top_k=24)
        _t['t'] = sherpa_onnx.AudioTagging(cfg)
    return _t['t']


def tag(x, sr=16000, win=1.6, hop=.5):
    """-> dict of curves at `hop` seconds, or None."""
    if not tagger_ready() or len(x) < sr:
        return None
    T = _tagger()
    n = max(1, int(np.ceil((len(x) / sr - win * .5) / hop)))
    out = {k: np.zeros(n) for k in ('laugh', 'scream', 'crowd', 'cry', 'music')}
    sets = {'laugh': LAUGH, 'scream': SCREAM, 'crowd': CROWD, 'cry': CRY, 'music': MUSIC}
    pk = float(np.percentile(np.abs(x), 99.5)) or 1.0
    for i in range(n):
        seg = x[int(i * hop * sr):int((i * hop + win) * sr)]
        if len(seg) < sr * .5:
            break
        s = T.create_stream()
        s.accept_waveform(sr, (seg * min(4.0, .6 / pk)).astype(np.float32))
        for e in T.compute(s):
            for k, names in sets.items():
                if e.name in names:
                    out[k][i] = max(out[k][i], e.prob)
    return {'hop': hop, 'offset': win / 2, **{k: np.round(v, 2).tolist() for k, v in out.items()}}


def music_report(tags, talk_share=0.0):
    """Sustained music likelihood with spans."""
    if not tags:
        return None
    m = np.array(tags['music'])
    on = np.convolve(m, np.ones(5) / 5, mode='same') > .42
    spans, st = [], None
    for i, v in enumerate(list(on) + [False]):
        if v and st is None:
            st = i
        elif not v and st is not None:
            if (i - st) * tags['hop'] >= 2.5:
                spans.append([round(st * tags['hop'], 1), round(i * tags['hop'] + 1, 1)])
            st = None
    return {'score': round(float(np.percentile(m, 80)), 2), 'spans': spans[:8]}


# ---------------------------------------------------------------------- chat
_LOL = re.compile(r'(?i)(l+m+f*a+o+|\bl+o+l+\b|lulw?|kekw?|omegalul|ha(ha)+|icant|\bdead\b|crying|im done|funny|\bxd+\b|pog|goated)')
_WOW = re.compile(r'(?i)(a+y+o{2,}|\bna+h{2,}|\bw+t+f+\b|wha+t{2,}|bro+ what|\bno way|omg+|oh+ (my|shit)|\?{3,}|yo{3,}|what the|holy|caught|wild|crazy|insane|\bsus\b|pause)')


def screen_text(path, dur, n=4):
    """What can be read on the finished Short. -> {'slur': bool, 'swear': [words], 'boxes': average number of text lines} or None."""
    if not ocr_ready():
        return None
    from . import speech
    if 'o' not in _o:
        from rapidocr_onnxruntime import RapidOCR
        _o['o'] = RapidOCR(intra_op_num_threads=6, det_limit_side_len=640, det_limit_type='max')
    slur, swear, boxes = False, set(), []
    for k in range(n):
        try:
            im = media.grab(path, dur * (k + .6) / n, 1080)
            import cv2
            res, _ = _o['o'](cv2.resize(im, (1080, 1920), interpolation=cv2.INTER_AREA), use_cls=False)
        except Exception:
            continue
        ts = [t for b, t, sc in res or [] if sc >= .6 and len(t) > 1]
        boxes.append(len(ts))
        for t in ts:
            slur |= speech.title_has_slur(t)
            swear |= {w.lower() for w in re.findall(r'(?i)(fuck\w*|shit\w*|bitch\w*|pussy|cunt)', t)}
    return {'slur': slur, 'swear': sorted(swear), 'boxes': round(sum(boxes) / max(1, len(boxes)), 1)}


def chat_events(path, meta, rect, every=2.4):
    """New chat lines over time (usernames dropped). -> [{t, lol, wow, n, text}]"""
    if not ocr_ready() or not rect:
        return None
    if 'o' not in _o:
        from rapidocr_onnxruntime import RapidOCR
        _o['o'] = RapidOCR(intra_op_num_threads=6, det_limit_side_len=640, det_limit_type='max')
    W, H = meta['w'], meta['h']
    x0, y0 = int(max(0, rect[0] - .025) * W), int(max(0, rect[1] - .012) * H)
    w, h = int(min(W - x0, (rect[2] + .08) * W)) // 2 * 2, int(min(H - y0, (rect[3] + .03) * H)) // 2 * 2
    if w < 80 or h < 40:
        return None
    k = 1.0 if H >= 1000 else 1.5
    ow, oh = int(w * k) // 2 * 2, int(h * k) // 2 * 2
    raw = media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', path, '-an', '-vf', f'fps={1 / every:.5f},crop={w}:{h}:{x0}:{y0},scale={ow}:{oh}:flags=bicubic,format=bgr24', '-f', 'rawvideo', 'pipe:1']).stdout
    fr = np.frombuffer(raw, np.uint8)
    fr = fr[:len(fr) // (ow * oh * 3) * (ow * oh * 3)].reshape(-1, oh, ow, 3)
    recent, out = [], []
    for i, im in enumerate(fr[:25]):
        media.check_cancel()
        res, _ = _o['o'](im, use_cls=False)
        new = []
        for b, txt, sc in res or []:
            if sc < .5 or len(txt) < 2:
                continue
            t = re.sub(r'^[^:：]{0,26}[:：]\s*', '', txt).strip() if re.search(r'[:：]', txt[:27]) else txt.strip()
            t = ''.join(c for c in t if ord(c) < 128).strip()
            if len(t) < 2 or sum(c.isalpha() for c in t) < 2 or re.match(r'^@?\w+\d{2,}$', t):
                continue
            key = re.sub(r'[^a-z]', '', t.lower())
            if not key or any(difflib.SequenceMatcher(None, key, r).ratio() > .78 for r in recent[-26:]):
                continue
            recent.append(key)
            new.append(t)
        if i == 0:
            continue          # the first screenful was there before the clip began
        out.append({'t': round((i + .5) * every, 1), 'n': len(new), 'lol': sum(bool(_LOL.search(t)) for t in new), 'wow': sum(bool(_WOW.search(t)) for t in new), 'text': new[:8]})
    return out


def text_boxes(im):
    """OCR boxes in an image -> [(x0,y0,x1,y1,text)] in pixels."""
    if not ocr_ready():
        return []
    if 'o' not in _o:
        from rapidocr_onnxruntime import RapidOCR
        _o['o'] = RapidOCR(intra_op_num_threads=6, det_limit_side_len=640, det_limit_type='max')
    res, _ = _o['o'](im, use_cls=False)
    out = []
    for box, txt, sc in res or []:
        xs, ys = [q[0] for q in box], [q[1] for q in box]
        if sc > .45:
            out.append((min(xs), min(ys), max(xs), max(ys), txt))
    return out


def find_chat_by_text(path, meta):
    """Chat in the top corners that motion alone did not reveal (busy or bright backgrounds)."""
    W, H = meta['w'], meta['h']
    best = None
    for side in (0, 1):
        x0 = 0 if side == 0 else int(.66 * W)
        rows, frames = [], 0
        for f in (.25, .5, .75):
            try:
                fr = media.grab(path, meta['duration'] * f)
            except media.MediaError:
                continue
            crop = fr[:int(.30 * H), x0:x0 + int(.34 * W)]
            t = [q for q in text_boxes(crop) if 10 <= q[3] - q[1] <= .045 * H and q[2] - q[0] > .02 * W and (':' in q[4] or '：' in q[4])]
            if len(t) >= 2:
                frames += 1
                rows += [q for q in text_boxes(crop) if q[3] - q[1] <= .05 * H]
        if frames >= 2 and rows:
            r = [(min(q[0] for q in rows) + x0) / W, min(q[1] for q in rows) / H, 0, 0]
            r[2] = (max(q[2] for q in rows) + x0) / W - r[0]
            r[3] = max(q[3] for q in rows) / H - r[1]
            r = [round(max(0, r[0] - .006), 4), round(max(0, r[1] - .008), 4), round(min(.36, r[2] + .012), 4), round(min(.30, r[3] + .016), 4)]
            if best is None or frames > best[0]:
                best = (frames, r)
    return best[1] if best else None


def chat_curves(events, n, grid):
    """Laughing-chat and hype curves on the analysis grid, shifted back to the moment that caused them."""
    lol, wow = np.zeros(n), np.zeros(n)
    for e in events or []:
        i = int(max(0, e['t'] - 2.6) / grid)
        j = min(n, i + int(1.8 / grid))
        if i < n:
            lol[i:j] += min(1.0, e['lol'] / 2)
            wow[i:j] += min(1.0, e['wow'] / 3)
    return np.clip(lol, 0, 1), np.clip(wow, 0, 1)
