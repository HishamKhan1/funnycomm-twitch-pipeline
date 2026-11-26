"""Advisory policy-risk screening. Never a guarantee; the user always reviews.

Levels
  clear    nothing found (still needs human review)
  caution  reviewer must look at listed moments
  hold     not built automatically; needs an explicit per-clip override
"""
from __future__ import annotations
import cv2
import numpy as np
from . import speech

_net = {}
EXPLICIT = {'FEMALE_GENITALIA_EXPOSED': 'exposed genitals', 'MALE_GENITALIA_EXPOSED': 'exposed genitals',
            'ANUS_EXPOSED': 'exposed buttocks', 'BUTTOCKS_EXPOSED': 'exposed buttocks', 'FEMALE_BREAST_EXPOSED': 'exposed breasts'}
SUGGESTIVE = {'FEMALE_BREAST_COVERED': 'swimwear / underwear', 'BUTTOCKS_COVERED': 'swimwear / underwear',
              'FEMALE_GENITALIA_COVERED': 'swimwear / underwear'}
SKIN = {'BELLY_EXPOSED', 'MALE_BREAST_EXPOSED', 'ARMPITS_EXPOSED', 'BELLY_COVERED'}
# Measured on real clips: ordinary gameplay 0-4 % of frames, sexualised scenes 16-86 %.
SKIN_HOLD, SKIN_CHECK = .14, .06


def nude_ready():
    try:
        import nudenet, onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _detector():
    if 'd' not in _net:
        from nudenet import NudeDetector
        _net['d'] = NudeDetector()
    return _net['d']


def _lit(crop):
    """Colour-cast and dark scenes (red clubs, neon rooms) rebuilt as even-lit grey."""
    m = crop.reshape(-1, 3).mean(axis=0) + 1
    if m.max() / m.min() < 1.45 and m.mean() > 62:
        return None
    g = cv2.createCLAHE(3, (6, 6)).apply(crop.max(axis=2))
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def scan_frames(hi, rate, webcam=None, max_frames=64):
    """Nudity/suggestive detections -> {'n': frames, 'hits': [{t,label,kind,score,box}]}."""
    if not nude_ready():
        return None
    det = _detector()
    n, H, W = hi.shape[:3]
    step = max(1, int(np.ceil(n / max_frames)))
    hits, frames, ff, fm = [], 0, 0, 0
    cx0, cx1 = int(.18 * W), int(.82 * W)
    for i in range(0, n, step):
        f = hi[i]
        frames += 1
        views = [(f, 0, 0, 1.0), (f[:, cx0:cx1], cx0, 0, 1.0)]
        lit = _lit(f[:, cx0:cx1])
        if lit is not None:
            views.append((lit, cx0, 0, .9))
        seen = []
        has_f = has_m = False
        for im, ox, oy, trust in views:
            for d in det.detect(np.ascontiguousarray(im)):
                lab, sc = d['class'], float(d['score']) * trust
                if trust == 1.0 and sc > .45:
                    has_f |= lab == 'FACE_FEMALE'
                    has_m |= lab == 'FACE_MALE'
                kind = 'explicit' if lab in EXPLICIT else 'suggestive' if lab in SUGGESTIVE else 'skin' if lab in SKIN else None
                if not kind or sc < (.32 if kind == 'explicit' else .33 if kind == 'skin' else .40):
                    continue
                x, y, a, b = d['box']
                box = [(x + ox) / W, (y + oy) / H, a / W, b / H]
                area = box[2] * box[3]
                if area < (.004 if kind == 'skin' else .0012):
                    continue
                in_cam = False
                if webcam:
                    wx, wy, ww, wh = webcam['rect']
                    mx, my = box[0] + box[2] / 2, box[1] + box[3] / 2
                    in_cam = wx <= mx <= wx + ww and wy <= my <= wy + wh
                if any(s[0] == lab and abs(s[1] - box[0]) < .06 and abs(s[2] - box[1]) < .08 for s in seen):
                    continue
                seen.append((lab, box[0], box[1]))
                hits.append({'t': round((i + .5) / rate, 2), 'label': lab, 'kind': kind, 'score': round(sc, 2),
                             'box': [round(v, 4) for v in box], 'cam': in_cam})
        ff += has_f
        fm += has_m
    return {'n': frames, 'hits': hits, 'f': round(ff / max(1, frames), 2), 'm': round(fm / max(1, frames), 2)}


def spans(times, gap=1.6, pad=.6, dur=None):
    """Merge sample times into padded [start,end] spans."""
    out = []
    for t in sorted(times):
        a, b = max(0, t - pad), t + pad
        if out and a <= out[-1][1] + gap - pad:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    if dur:
        out = [[round(a, 2), round(min(b, dur), 2)] for a, b in out]
    return out


def _weight(h, irl):
    """Known confusions: male chests read as breasts; forearms and hands as genitals on real cameras."""
    if h['label'] == 'FEMALE_BREAST_EXPOSED' and (irl or h['score'] < .58):
        return .55
    if irl and 'GENITALIA' in h['label']:
        return .4
    if irl:
        return .7
    return 1.0


def _union(sp):
    out = []
    for a, b in sorted(sp):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def assess(title, dur, nude_hits, words, speech_ok, music, has_audio, irl=False):
    """Combine signals into one report with evidence times."""
    reasons, level = [], 'clear'
    lang = speech.language_scan(words)
    explicit_t, sugg_t = [], []
    nsamp = None
    if isinstance(nude_hits, dict):
        nsamp, nude_hits = nude_hits['n'], nude_hits['hits']
    if nude_hits is None:
        reasons.append({'level': 'caution', 'tag': 'visual', 'text': 'Nudity scanner not installed; check visuals manually', 't': []})
    else:
        by_t = {}
        for h in nude_hits:
            by_t.setdefault(h['t'], []).append(h)
        raw = []
        for t, hs in by_t.items():
            ex = [h for h in hs if h['kind'] == 'explicit']
            if ex:
                raw.append((t, max(h['score'] * _weight(h, irl) for h in ex), sorted({EXPLICIT[h['label']] for h in ex})))
            su = [h for h in hs if h['kind'] == 'suggestive' and h['box'][2] * h['box'][3] > .004]
            if su:
                sugg_t.append(t)
        # Bare male chests are often mislabelled, so breast hits count for less.
        # Evidence has to persist: bare skin close-ups on a real camera trigger
        # single frames constantly, so real-world footage needs more of it.
        kinds = set()
        need = 1.7 if irl else .95
        for t, s, names in raw:
            if sum(o[1] for o in raw if abs(o[0] - t) <= 1.5) >= need:
                explicit_t.append(t)
                kinds.update(names)
    ex_spans = spans(explicit_t, dur=dur)
    # T-shirts and hoodies trip the detector on real cameras, so one-off hits there are ignored.
    su_spans = spans(sugg_t, dur=dur) if len(sugg_t) >= (max(5, .14 * (nsamp or 30)) if irl else 2) else []
    # A censored moment also covers the suggestive frames that run into it.
    for e in ex_spans:
        for a, z in su_spans:
            if a <= e[1] + .3 and z >= e[0] - .3:
                e[0] = round(max(a, e[0] - 1.6), 2)
                e[1] = round(min(z, e[1] + 1.6), 2)
    ex_sec = sum(b - a for a, b in ex_spans)
    if ex_spans:
        share = ex_sec / max(dur, .1)
        mixed = sum(b - a for a, b in _union(ex_spans + su_spans)) / max(dur, .1)
        if share > .30 or ex_sec > 8 or (not irl and mixed > .34):
            reasons.append({'level': 'hold', 'tag': 'nudity', 'text': f'Nudity / sexual scene ({", ".join(sorted(kinds))}) across {mixed * 100:.0f}% of the clip', 't': ex_spans})
        else:
            reasons.append({'level': 'caution', 'tag': 'nudity', 'text': f'Possible nudity ({", ".join(sorted(kinds))}); these moments are censored in the edit', 't': ex_spans})
    if nsamp and not irl and nude_hits:
        skin_t = sorted({h['t'] for h in nude_hits if not h['cam'] and h['box'][2] * h['box'][3] >= .004})
        sh = len(skin_t) / nsamp
        if sh >= SKIN_HOLD and len(skin_t) >= 3 and not any(r['level'] == 'hold' for r in reasons):
            reasons.append({'level': 'hold', 'tag': 'sexual_scene', 'text': f'Game scene with bare skin or sexual content in {sh * 100:.0f}% of frames', 't': spans(skin_t, dur=dur)})
        elif sh >= SKIN_CHECK and not su_spans:
            reasons.append({'level': 'caution', 'tag': 'suggestive', 'text': 'Some bare skin in the game footage; judge whether it is too sexualised', 't': spans(skin_t, dur=dur)})
    if su_spans:
        reasons.append({'level': 'caution', 'tag': 'suggestive', 'text': 'Suggestive visuals (swimwear / underwear); judge whether it is too sexualised', 't': su_spans})
    if lang['slur']:
        reasons.append({'level': 'caution', 'tag': 'slur', 'text': f'{len(lang["slur"])} possible slur(s) heard; muted in the edit, confirm by listening', 't': [[a, b] for a, b, _ in lang['slur']]})
    if speech.title_has_slur(title):
        reasons.append({'level': 'hold', 'tag': 'slur', 'text': 'The Twitch title says a slur is used in this clip', 't': []})
    if lang['strong']:
        first = lang['strong'][0][0]
        reasons.append({'level': 'info', 'tag': 'profanity', 'text': f'Strong language x{len(lang["strong"])}' + (' (within first 8s)' if first < 8 else ''), 't': [[a, b] for a, b, _ in lang['strong'][:6]]})
    for key, label in (('sexual', 'Sexual language'), ('violent', 'Violent wording'), ('drugs', 'Drug reference'), ('gambling', 'Gambling reference')):
        if len(lang[key]) >= (1 if key != 'violent' else 2):
            reasons.append({'level': 'caution' if key in ('sexual', 'gambling') else 'info', 'tag': key, 'text': f'{label} x{len(lang[key])}', 't': [[a, b] for a, b, _ in lang[key][:6]]})
    if music and music.get('score', 0) >= .5 and music.get('spans'):
        reasons.append({'level': 'caution', 'tag': 'music', 'text': 'Possible background music; listen for copyrighted songs', 't': music['spans']})
    if has_audio and not speech_ok:
        reasons.append({'level': 'caution', 'tag': 'audio', 'text': 'Speech was not transcribed; listen to all audio', 't': []})
    if not has_audio:
        reasons.append({'level': 'caution', 'tag': 'audio', 'text': 'No audio track', 't': []})
    order = {'clear': 0, 'info': 0, 'caution': 1, 'hold': 2}
    for r in reasons:
        if order[r['level']] > order[level]:
            level = r['level']
    return {'level': level, 'reasons': reasons, 'censor': ex_spans, 'suggestive': su_spans,
            'mute': [[a, b] for a, b, _ in lang['slur']], 'language': {k: len(v) for k, v in lang.items()}}


def refresh_language(an, title=''):
    """Re-judge stored words with the current language rules. True when something changed."""
    rep = an['safety']
    lang = speech.language_scan(an['speech']['words'])
    mute = [[a, b] for a, b, _ in lang['slur']]
    want = []
    if len(mute) >= 3:
        want.append({'level': 'hold', 'tag': 'slur', 'text': f'A slur, or a word that sounds like one, is said {len(mute)} times', 't': mute[:6]})
    elif mute:
        want.append({'level': 'caution', 'tag': 'slur', 'text': f'{len(mute)} possible slur(s) heard; muted in the edit, confirm by listening', 't': mute})
    if speech.title_has_slur(title or an.get('title')):
        want.append({'level': 'hold', 'tag': 'slur', 'text': 'The Twitch title says a slur is used in this clip', 't': []})
    old = [r for r in rep['reasons'] if r['tag'] == 'slur']
    if old == want and rep['mute'] == mute:
        return False
    rep['reasons'] = [r for r in rep['reasons'] if r['tag'] != 'slur'] + want
    rep['mute'] = mute
    order = {'clear': 0, 'info': 0, 'caution': 1, 'hold': 2}
    rep['level'] = max(['clear'] + [r['level'] for r in rep['reasons']], key=lambda v: order[v])
    rep['level'] = 'clear' if rep['level'] == 'info' else rep['level']
    return True


def flags(report):
    """Flat strings for the legacy flags column / review text."""
    out = []
    for r in report.get('reasons', []):
        when = ''
        if r['t']:
            when = ' @ ' + ', '.join(f'{a:.0f}-{b:.0f}s' for a, b in r['t'][:4])
        out.append(('HOLD: ' if r['level'] == 'hold' else 'CHECK: ' if r['level'] == 'caution' else 'Note: ') + r['text'] + when)
    return out
