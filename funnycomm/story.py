"""Joke structure, trimming, scoring and compilation planning.

The "payoff" is where the room reacts: loud/bright voice, streamer movement,
chat bursting and laughter. Cuts are then built around it so the setup survives
and nothing starts or stops in the middle of a word.
"""
from __future__ import annotations
import difflib, json, re
from datetime import datetime, timezone
import numpy as np
from .audio import smooth, norm01


# ------------------------------------------------------------------- moments
def _arr(an, k):
    return np.asarray(an['curves'][k], float)


def fused(an):
    g = an['grid']
    ex = _arr(an, 'audio')
    n = len(ex)
    has_cam = bool(an['layout']['cam'])
    cam = norm01(smooth(_arr(an, 'cam'), 5)) if has_cam else np.zeros(n)
    mot = norm01(smooth(_arr(an, 'motion'), 5), 40, 97)
    chat = np.zeros(n)
    if an['layout']['chat']:
        c = smooth(_arr(an, 'chat'), int(1.6 / g))
        base = smooth(c, int(8 / g))
        d = np.clip(c - base, 0, None)
        chat = norm01(d, 30, 97)
        k = int(1.8 / g)                       # chat reacts ~2 s after the moment
        chat = np.r_[chat[k:], np.zeros(min(k, n))][:n]
    lg = _arr(an, 'laugh')
    talk = np.zeros(n)
    for w, s, e, p in an['speech']['words']:
        talk[int(s / g):int(e / g) + 1] = 1
    if an.get('tagged'):
        # Real laughter, screams and laughing chat outrank plain loudness.
        lol, wow, scr = _arr(an, 'chat_lol'), _arr(an, 'chat_wow'), _arr(an, 'scream')
        f = (.62 * np.clip(smooth(ex, 5), 0, 1.6) + .45 * cam + .18 * chat + 1.05 * smooth(np.clip((lg - .08) / .5, 0, 1), 5) + .50 * smooth(scr, 5)
             + .70 * smooth(lol, 9) + .28 * smooth(wow, 9) + (.08 if has_cam else .20) * mot + .10 * smooth(talk, 5))
        return f, {'ex': ex, 'cam': cam, 'mot': mot, 'chat': chat, 'laugh': np.clip((lg - .08) / .5, 0, 1), 'talk': talk, 'lol': lol, 'wow': wow, 'scream': scr}
    f = 1.0 * np.clip(smooth(ex, 5), 0, 1.6) + .72 * cam + .30 * chat + .35 * smooth(lg, 5) + (.12 if has_cam else .28) * mot + .12 * smooth(talk, 5)
    return f, {'ex': ex, 'cam': cam, 'mot': mot, 'chat': chat, 'laugh': lg, 'talk': talk}


def moments(an):
    g = an['grid']
    f, parts = fused(an)
    n = len(f)
    dur = an['meta']['duration']
    t = (np.arange(n) + .5) * g
    pos = np.interp(t / max(dur, .1), [0, .15, .42, .92, .98, 1], [.55, .74, 1, 1, .9, .55])
    s = smooth(f, 5) * pos
    k = int(np.argmax(s))
    peak = float(s[k])
    on = k
    while on > 0 and s[on - 1] > .62 * peak and (k - on) * g < 1.2:
        on -= 1
    thr = max(.55 * peak, float(s.mean() + .8 * s.std()))
    beats, order = [], np.argsort(-s)
    for i in order:
        if s[i] < thr or len(beats) >= max(3, int(dur / 4)):
            break
        if all(abs(i - b) * g >= 2.2 for b in beats):
            beats.append(int(i))
    rows = [{'t': round(float(t[b]), 2), 's': round(float(s[b] / max(peak, 1e-6)), 2)} for b in sorted(beats)]
    jokes = punchlines(an, f, parts)
    if jokes and jokes[0]['v'] >= .42:
        # Land on the line itself, not on the noise after it.
        j = jokes[0]
        pt = max(j['s'], j['e'] - .35)
        if not any(abs(q['t'] - pt) < .5 for q in rows):
            rows = sorted(rows + [{'t': round(pt, 2), 's': 1.0}], key=lambda q: q['t'])
        return {'payoff': round(float(pt), 2), 'onset': round(float(j['s']), 2), 'peak': round(peak, 3), 'beats': rows, 'mean': round(float(f.mean()), 3),
                'fused': [round(float(v), 3) for v in smooth(f, 3)], 'jokes': jokes[:5], 'source': 'punchline'}
    return {'payoff': round(float(t[k]), 2), 'onset': round(float(t[on]), 2), 'peak': round(peak, 3), 'beats': rows,
            'mean': round(float(f.mean()), 3), 'fused': [round(float(v), 3) for v in smooth(f, 3)], 'jokes': jokes[:5], 'source': 'reaction'}


# ------------------------------------------------------------------ sentences
def sentences(words, gap=.42):
    out, cur = [], []
    for w in words:
        if cur and (w[1] - cur[-1][2] > gap or re.search(r'[.?!]$', cur[-1][0])):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return [{'s': c[0][1], 'e': c[-1][2], 'text': ' '.join(x[0] for x in c), 'p': float(np.mean([x[3] for x in c]))} for c in out]


def _key(t):
    return re.sub(r'[^a-z ]', '', t.lower()).strip()


def punchlines(an, f, parts):
    """Score every spoken line by what it caused: laughs, chat laughing, a jump in energy, a reply."""
    g, dur = an['grid'], an['meta']['duration']
    sent = sentences(an['speech']['words'])
    if not sent:
        return []
    lg, ex = parts['laugh'], parts['ex']
    lol, wow, scr = parts.get('lol'), parts.get('wow'), parts.get('scream')

    def mx(a, t0, t1):
        if a is None:
            return 0.0
        i, j = int(max(0, t0) / g), max(int(max(0, t0) / g) + 1, int(min(dur, t1) / g))
        return float(a[i:j].max()) if i < len(a) else 0.0

    def mean(a, t0, t1):
        i, j = int(max(0, t0) / g), max(int(max(0, t0) / g) + 1, int(min(dur, t1) / g))
        return float(a[i:j].mean()) if i < len(a) else 0.0
    out = []
    keys = [_key(x['text']) for x in sent]
    for i, x in enumerate(sent):
        laugh, chat = mx(lg, x['e'] - .3, x['e'] + 3.0), mx(lol, x['e'] - .5, x['e'] + 2.2)
        jump = float(np.clip(mean(ex, x['e'] - .6, x['e'] + 1.0) - mean(ex, x['s'] - 2.2, x['s']), 0, 1))
        reply = i > 0 and sent[i - 1]['text'].rstrip().endswith('?') and x['s'] - sent[i - 1]['e'] < 2.0
        reps = sum(1 for k in keys[:i] if k and difflib.SequenceMatcher(None, k, keys[i]).ratio() > .82)
        v = (1.0 * laugh + .85 * chat + .45 * mx(scr, x['s'], x['e'] + 1.5) + .30 * mx(wow, x['e'] - .5, x['e'] + 2.2) + .30 * jump + .18 * reply + .07 * min(4, reps)
             + .08 * ('!' in x['text']) + .10 * float(np.interp(x['e'] / max(dur, .1), [0, .3, .9, 1], [0, .4, 1, .9])))
        out.append({'s': round(float(x['s']), 2), 'e': round(float(x['e']), 2), 'v': round(float(v), 3), 'text': x['text'][:120], 'laugh': round(laugh, 2), 'chat': round(chat, 2), 'reps': reps})
    return sorted(out, key=lambda q: -q['v'])


def dialogue(an):
    """0..1 how much real back-and-forth there is (questions answered, quick turns)."""
    sent = sentences(an['speech']['words'])
    qa = sum(1 for a_, b_ in zip(sent, sent[1:]) if a_['text'].rstrip().endswith('?') and b_['s'] - a_['e'] < 2.2)
    quick = sum(1 for a_, b_ in zip(sent, sent[1:]) if b_['s'] - a_['e'] < 1.4)
    dur = max(1.0, an['meta']['duration'])
    return float(np.clip(.17 * qa + .035 * quick + .25 * np.clip(len(sent) / dur * 4, 0, 1), 0, 1))


def setup_start(an, payoff, floor):
    """Where the bit that leads to this payoff begins: follow the talk back to a real pause."""
    g = an['grid']
    sent = [x for x in sentences(an['speech']['words']) if x['s'] < payoff + .2]
    f = np.asarray(an['moments']['fused'])
    peak = max(an['moments']['peak'], .05)
    if not sent:
        return None
    k = len(sent) - 1
    while k > 0:
        gap = sent[k]['s'] - sent[k - 1]['e']
        if sent[k - 1]['s'] < floor:
            break
        quiet = f[int(sent[k - 1]['e'] / g):int(sent[k]['s'] / g) + 1]
        if gap > 3.2 and len(quiet) and quiet.mean() < .30 * peak and payoff - sent[k]['s'] > 3.0:
            break
        k -= 1
    at = sent[k]['s']
    if at < floor - .01:
        # One long unbroken run of talk: join it at a word, inside the length budget.
        at = min((w[1] for w in an['speech']['words'] if w[1] >= floor), default=floor)
        return max(0.0, at - .06)
    return max(0.0, at - .30)


def _inside_word(words, t):
    for w, s, e, p in words:
        if s + .02 < t < e - .02:
            return s, e
    return None


def clean_open(an, a, payoff, words):
    """Never open on a face cut off by the frame or pressed against the lens: step forward to the first clean picture."""
    if an['layout'].get('cam') or not an.get('tracks'):
        return a

    def messy(t):
        m = 0.0
        for tr in an['tracks']:
            brief = len(tr['t']) and tr['t'][-1] - tr['t'][0] < 3.0          # somebody passing the lens, not a selfie camera
            for u, cx, w in zip(tr['t'], tr['cx'], tr['w']):
                if abs(u - t) < .2:
                    edge = max(0.0, .55 * w - cx, cx + .55 * w - 1) if w > .12 else 0.0
                    m = max(m, (w - .18) * brief if w > .18 else 0.0, w - .34, edge)
        return m
    here = messy(a + .1)
    if not here:
        return a
    best, bm = a, here
    t = a
    while t + .1 < min(a + 1.6, payoff - 2.5):
        t += .1
        if _inside_word(words, t):
            continue
        m = messy(t + .1)
        if not m:
            return round(t, 2)
        if m < bm * .6:
            best, bm = round(t, 2), m
    return best


def cut(an, max_len, kind='single', tail_max=None, window=None):
    """Choose kept source ranges. Returns {'keep': [[a,b],...], 'payoff': t, 'why': [...]}"""
    g, dur = an['grid'], an['meta']['duration']
    m = an['moments']
    f = np.asarray(m['fused'])
    words = an['speech']['words']
    sent = sentences(words)
    why = []
    payoff, peak = m['payoff'], max(m['peak'], .05)
    if window:
        a, b = max(0, window[0]), min(dur, window[1])
        inside = [x for x in m['beats'] if a <= x['t'] <= b]
        if inside and not a <= payoff <= b:
            payoff = max(inside, key=lambda x: x['s'])['t']
        return {'keep': [[round(a, 3), round(b, 3)]], 'payoff': payoff, 'why': ['Manual in/out points']}
    max_setup = (max_len - 4) if kind in ('single', 'video') else max(5.0, max_len - 3.5)
    tail_max = tail_max or {'single': 6.5, 'video': 14.0}.get(kind, 5.0)

    def level(a, b):
        i, j = int(max(0, a) / g), max(int(max(0, a) / g) + 1, int(min(dur, b) / g))
        return float(f[i:j].mean()) if i < len(f) else 0.0
    # ---- start
    floor = max(0.0, payoff - max_setup)
    starts = [0.0] if floor <= .05 else []
    starts += [max(0.0, s['s'] - .18) for s in sent if floor <= s['s'] - .18 < payoff - .5]
    i = int(floor / g)
    while i < int(payoff / g) - 8:             # activity onsets after a lull
        if f[i] > .42 * peak and level(i * g - 1.0, i * g) < .24 * peak:
            starts.append(max(0.0, i * g - .25))
            i += 10
        i += 1
    starts = sorted(set(round(s, 2) for s in starts)) or [floor]
    if dur <= 13:
        starts = [0.0] + [s for s in starts if s > .4 and level(0, s) < .16 * peak and not any(x['s'] < s - .1 for x in sent)][:1]
    quiet = .20 * peak
    pick = starts[0]
    ss = setup_start(an, payoff, floor) if dur > 13 else None
    if ss is not None and (kind != 'video' or payoff - ss > 6):
        # Never skip movement that clearly belongs to the moment.
        pre = level(0, ss) if ss > .5 else 0
        starts = [ss] if pre < .55 * peak or ss < 1.2 or floor > 0 else starts
        pick = starts[0]
    for s in starts:
        later = [q for q in starts if q > s + .4]
        talking = any(x['s'] < s + 1.6 and x['e'] > s for x in sent)
        if not talking and level(s, s + 1.7) < quiet and later and payoff - later[0] > .8:
            continue
        pick = s
        break
    a = pick
    iw = _inside_word(words, a)
    if iw:
        a = max(0.0, iw[0] - .08)
    a = clean_open(an, a, payoff, words)
    if a > .6:
        why.append(f'Opens at {a:.1f}s where the setup begins')
    # ---- end
    # Whoever made the clip chose its ending, so the tail is kept unless it is
    # dead or the cut would be too long. Rankings need a tighter tail.
    last_talk = max((x['e'] for x in sent), default=0.0)
    alive = dur
    while dur > 13 and alive > payoff + 1.4 and level(alive - 1.0, alive) < .24 * peak and alive - .2 > last_talk + .25:
        alive -= .2
    b = alive if dur - alive > .6 else dur
    if b < dur:
        why.append(f'Trimmed {dur - b:.1f}s of dead tail')
    if b - a > max_len or (kind == 'ranking' and b > payoff + tail_max + 3.5):
        e_min = min(dur, payoff + (1.3 if kind != 'ranking' else 1.0))
        e_cap = min(b, payoff + tail_max, a + max_len)
        strong_next = [x['t'] for x in m['beats'] if payoff + .8 < x['t'] < payoff + tail_max + 2 and x['s'] > .68]
        if strong_next and a + max_len > strong_next[-1] + 1.0:
            e_min = max(e_min, min(dur, strong_next[-1] + .9))
            e_cap = min(b, max(e_cap, strong_next[-1] + 2.0), a + max_len)
            why.append('Keeps the follow-up reaction')
        b2, t = e_cap, e_min
        while t < e_cap:
            busy = any(x['s'] - .05 < t < x['e'] + .12 for x in sent)
            if not busy and level(t, t + .6) < .34 * peak:
                b2 = t + .18
                break
            t += .1
        b = b2
        if dur - b > .8:
            why.append(f'Ends {b - payoff:.1f}s after the payoff, before it goes flat')
    iw = _inside_word(words, b)
    if iw:
        b = iw[1] + .12 if iw[1] + .12 - a <= max_len + 1.2 else iw[0] - .04
    floor_len = min(dur, {'single': 7.0, 'video': 8.0}.get(kind, 5.0))
    if b - a < floor_len:
        b = min(dur, max(b, a + floor_len))
        a = max(0.0, min(a, b - floor_len))
    b = float(np.clip(b, a + min(2.2, dur - a), dur))
    # ---- dead air inside, never touching the beat right before the payoff
    keep, cur = [], a
    if b - a > 9:
        marks = [(x['s'], x['e']) for x in sent if x['e'] > a and x['s'] < b]
        edges = [a] + [v for p in marks for v in p] + [b]
        for gs, ge in zip(edges[::2], edges[1::2]):
            gs, ge = max(gs, a), min(ge, b)
            # A pause is often the joke. Only clearly dead gaps go, and wide shots keep more air.
            if ge - gs < (3.4 if kind == 'video' else 2.4) or gs < payoff + 1.2 and ge > payoff - 3.0:
                continue
            if level(gs + .4, ge - .4) < .13 * peak and gs - cur > 1.6:
                pad = .7 if kind == 'video' else .45
                keep.append([cur, gs + pad])
                cur = ge - pad
                why.append(f'Tightened {ge - gs - .7:.1f}s of dead air at {gs:.1f}s')
    keep.append([cur, b])
    keep = [[round(float(x), 3), round(float(y), 3)] for x, y in keep if y - x > .25]
    return {'keep': keep, 'payoff': round(payoff, 2), 'why': why}


def length(keep):
    return sum(b - a for a, b in keep)


# -------------------------------------------------------------------- scoring
_GRAVE = ['failed', 'depress\\w*', 'passed away', 'died', 'death', 'funeral', 'hospital', 'cancer', 'crying', 'cried', 'god had', 'worked (so|very) hard', 'i tried my best', 'my (mom|dad|brother|sister) (is|was)',
          'proud of', 'grateful', 'therapy', 'anxiety', 'broke up', 'heartbroken', 'sorry for your loss', 'mental health', 'not gonna lie i cried', 'disappointed', 'prayed', 'other plans']


def score(an, src, peers=None, rating=0, weights=None):
    """0-100 entertainment estimate with plain-language reasons.

    Deliberately not a view-count ranking: views contribute, but the clip's own
    reaction, setup and pace matter more, and your own star rating outranks both.
    """
    m, dur = an['moments'], an['meta']['duration']
    f = np.asarray(m['fused'])
    _, p = fused(an)
    why = []
    contrast = float(np.clip((m['peak'] - np.median(f)) / 1.35, 0, 1))
    gap = float(np.clip(((an['audio'].get('peak_gap_db') or 0) - 3) / 15, 0, 1))
    react = 100 * (.55 * contrast + .45 * gap)
    if react > 62:
        why.append('big reaction')
    laugh = 100 * float(np.clip((np.percentile(p['laugh'], 96) - .25) / .55, 0, 1)) if len(p['laugh']) else 0
    if laugh > 55:
        why.append('laughing')
    chat = 45.0
    if an['layout']['chat']:
        c = smooth(_arr(an, 'chat'), int(2.0 / an['grid']))
        burst = float(np.percentile(c, 96) / max(np.median(c), .004))
        chat = 100 * float(np.clip((burst - 1.25) / 1.9, 0, 1))
    if an['layout']['chat'] and chat > 65:
        why.append('chat explodes')
    words = an['speech']['words']
    before = [w for w in words if w[1] < m['payoff']]
    density = len(words) / max(dur, 1)
    setup = 100 * float(np.clip(len(before) / 9, 0, 1) * .6 + np.clip(density / 2.2, 0, 1) * .4)
    if setup > 65:
        why.append('clear setup')
    face = 45
    if an['layout']['cam']:
        sup = len(an['face_cam']) / max(1, min(180, dur * 4))
        face = 100 * float(np.clip(sup, 0, 1) * .5 + np.clip(p['cam'].max(), 0, 1) * .5)
    elif an['tracks']:
        face = 100 * float(np.clip(max(sum(t['w']) / len(t['w']) for t in an['tracks']) / .14, 0, 1))
    fit = float(np.interp(dur, [3, 6, 9, 28, 45, 70], [35, 70, 95, 100, 80, 60]))
    v = int(src.get('views') or 0)
    pop = 50.0
    if peers:
        vs = sorted(int(x.get('views') or 0) for x in peers)
        pop = 100 * (np.searchsorted(vs, v, 'right') / max(1, len(vs)))
        try:
            age = max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(src['created'].replace('Z', '+00:00'))).days)
            vel = sorted(int(x.get('views') or 0) / max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(x['created'].replace('Z', '+00:00'))).days) for x in peers if x.get('created'))
            pop = .55 * pop + .45 * 100 * (np.searchsorted(vel, v / age, 'right') / max(1, len(vel)))
        except (ValueError, KeyError, TypeError):
            pass
    if pop > 80:
        why.append('popular clip')
    # --- comedy: did anybody actually laugh, and at a line?
    jokes = m.get('jokes') or []
    punch = float(np.clip((jokes[0]['v'] - .12) / 1.25, 0, 1)) if jokes else 0.0
    lt = float(np.clip((np.asarray(p['laugh']) > .30).mean() * 4.0, 0, 1)) if len(p['laugh']) else 0.0
    lols = float(np.clip((np.asarray(p['lol']) > .3).mean() * 6, 0, 1)) if p.get('lol') is not None else .25
    dlg = dialogue(an)
    comedy = 100 * float(np.clip(.44 * punch + .24 * lt + .18 * lols + .22 * dlg, 0, 1))
    if comedy > 52:
        why.append('lands a joke' if punch > .4 else 'people laughing')
    if dlg > .55:
        why.append('good back-and-forth')
    ppl = max(1, int(src.get('people') or 1))
    crowd = 100 * float(np.clip(np.log1p(ppl - 1) / np.log(8), 0, 1))
    try:
        ct = json.loads(src.get('crowd_titles') or '[]')
    except ValueError:
        ct = []
    if ct:
        from . import index
        fl = index.text_flags(ct)
        crowd = float(np.clip(crowd + 14 * min(2, fl['fun']) - 25 * fl['sad'], 0, 100))
    if ppl >= 3:
        why.append(f'{ppl} viewers clipped it')
    w = weights or {}
    parts = {'comedy': (comedy, w.get('comedy', .32)), 'crowd': (crowd, w.get('crowd', .24)), 'reaction': (react, w.get('reaction', .12)), 'dialogue': (100 * dlg, w.get('dialogue', .10)),
             'face': (face, w.get('face', .05)), 'length': (fit, w.get('length', .05)), 'popularity': (pop, w.get('popularity', .12))}
    total = sum(a * b for a, b in parts.values()) / sum(b for _, b in parts.values())
    lvl = an['safety']['level']
    pen = 0
    tags = {r['tag'] for r in an['safety']['reasons'] if r['level'] in ('caution', 'hold')}
    pen += 14 * ('nudity' in tags) + 6 * ('suggestive' in tags) + 6 * ('slur' in tags) + 4 * ('sexual' in tags) + 5 * ('music' in tags)
    if lvl == 'hold':
        pen += 25
    if m['peak'] < .55 and not words:
        pen += 12
        why.append('quiet clip')
    # Not every loud or popular clip is comedy.
    txt = ' '.join(w[0] for w in words).lower()
    grave = sum(1 for g in _GRAVE if re.search(rf'\b{g}\b', txt))
    if grave >= 2 and comedy < 60:
        pen += 22
        why.append('serious or emotional, not comedy')
    if density > 1.7 and react < 62 and comedy < 34:
        pen += 10
        why.append('mostly calm talking')
    if an['layout']['mode'] == 'split' and 'Grand Theft Auto' in (src.get('game') or '') and comedy < 40:
        pen += 7
        why.append('mostly game cutscene')
    total = max(0, sum(a * b_ for a, b_ in parts.values()) / sum(b_ for _, b_ in parts.values()) - pen)
    if not jokes and react < 40 and ppl < 3:
        pen += 6
    if rating:
        total = .45 * total + .55 * (rating * 20)
        why.insert(0, f'your rating {rating}/5')
    return {'total': round(float(np.clip(total, 0, 100)), 1), 'parts': {k: round(float(a), 0) for k, (a, _) in parts.items()}, 'penalty': pen, 'why': why}


# ------------------------------------------------------------- ranked planning
def theme(src, an):
    """Group key for compilations: streamer + what is on screen."""
    game = (src.get('game') or '').lower()
    if 'grand theft auto' in game or re.search(r'\bgta\b', (src.get('title') or '').lower()):
        kind = 'gta'
    elif an['layout']['mode'] == 'split':
        kind = 'gaming'
    else:
        kind = 'irl'
    return src['streamer'], kind


def pick_rankings(items, size=3, max_groups=1):
    """items: [{'src','an','score'}...] -> list of groups, weakest first so #1 closes."""
    groups = {}
    for it in items:
        groups.setdefault(theme(it['src'], it['an']), []).append(it)
    merged = {}
    for (who, kind), rows in groups.items():   # thin themes fall back to the streamer as a whole
        if len(rows) >= size:
            merged[(who, kind)] = rows
        else:
            merged.setdefault((who, 'mixed'), []).extend(rows)
    out = []
    for key, rows in merged.items():
        if len(rows) < size:
            continue
        rows = sorted(rows, key=lambda r: -r['score']['total'])
        if rows[size - 1]['score']['total'] < 34:
            continue
        chosen = rows[:size]
        out.append({'key': key, 'items': chosen[::-1], 'strength': sum(r['score']['total'] for r in chosen) / size})
    out.sort(key=lambda g: -g['strength'])
    return out[:max_groups]
