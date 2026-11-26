"""Titles, headlines, ranking labels, descriptions and tags.

Only uses facts that are actually known (streamer, game, the clip's own title,
words that were heard, measured reaction). It offers several options; it does
not invent events. Every field is editable in the app before approval.
"""
from __future__ import annotations
import hashlib, json, re
from datetime import datetime, timezone
from . import speech
from .story import sentences

_JUNK = {'w', 'ww', 'www', 'l', 'a', 'b', 'c', 'd', 'e', 'x', 'wtf', 'omg', 'lol', 'lmao', 'lmfao', 'lolll', 'bro', 'bruh', 'yo', 'ayo', 'mar', 'ku',
         'clip', 'stream', 'this', 'guy', 'that', 'the', 'is', 'he', 'him', 'and', 'so', 'uh', 'uhhhh', 'ok', 'son', 'yikes', 'rizzler', 'dddd', 'gyat', 'gyatt',
         'nah', 'nahh', 'progress', 'mandy', 'highlight', 'live', 'twitch', 'untitled', 'pov', 'holy', 'no', 'context', 'what', 'whats', 'doing'}
_SMALL = {'a', 'an', 'the', 'and', 'or', 'of', 'in', 'on', 'to', 'for', 'at', 'is', 'with', 'vs', 'his', 'her', 'why', 'from'}
_TYPO = {'Marlon': ['malron', 'marlin', 'marln', 'marlom'], 'tucks': ['tuks'], 'called': ['callled', 'calld'], 'with': ['witj', 'wit'], 'film': ['flim'], 'jamming': ['jaming'], 'Jason': ['jasson'], 'trying': ['tryna', 'tryin'], 'because': ['becuase', 'bc'], 'that': ['taht'], 'Korea': ['korea'], 'GameStop': ['gamestop'], 'Lacy': ['lac', 'lacey', 'lacyy', 'lacyyy', 'lact'], 'impressed': ['imprerssed', 'impresed'], 'backboard': ['bacboared', 'backbord'], 'almost': ['alomoust', 'allmost'], 'mukbang': ['muckbang'], 'feeding': ['fedding'], 'tweaking': ['tweeking'], 'innocent': ['inncocent', 'inocent', 'innocnt'], 'restaurant': ['resturant', 'restaraunt'], 'happened': ['happend', 'happned'],
         'realizes': ['realises'], 'Marlon': ['malon', 'marlin'], 'Kilimanjaro': ['kilamanjaro'], 'both': ['bolth']}
GAME_SHORT = {'grand theft auto vi': 'GTA 6', 'grand theft auto v': 'GTA 5', 'counter-strike': 'CS2', 'valorant': 'VALORANT', 'fortnite': 'Fortnite',
              'league of legends': 'League', 'minecraft': 'Minecraft', 'rocket league': 'Rocket League', 'roblox': 'Roblox',
              "tom clancy's rainbow six siege x": 'R6 Siege', 'call of duty: warzone': 'Warzone', 'nba 2k26': 'NBA 2K26', 'ea sports fc 26': 'FC 26'}
NOT_GAMES = {'just chatting', 'irl', 'travel & outdoors', 'sports', 'special events', 'music', 'food & drink', 'fitness & health', 'asmr', 'pools, hot tubs, and beaches', 'the streamer awards', 'watch parties', 'events', 'talk shows & podcasts', 'animals, aquariums, and zoos', ''}


def display(login):
    login = (login or '').strip()
    return login[:1].upper() + login[1:] if login else 'Streamer'


def game_name(src):
    g = (src.get('game') or '').strip()
    if g.lower() in NOT_GAMES:
        t = (src.get('title') or '').lower()
        if re.search(r'\bgta\s*(6|vi)\b', t):
            return 'GTA 6'
        return ''
    g = re.sub(r'(?i)^ea sports fc (\d+)$', r'FC \1', g)
    return GAME_SHORT.get(g.lower(), g if len(g) <= 22 else '')


def tidy(title):
    """Human-readable version of a Twitch clip title, or '' if it says nothing."""
    t = re.sub(r'https?://\S+', ' ', title or '')
    t = re.sub(r'(?i)\b(clipped|created) with\b.*$', ' ', t)
    t = t.split('|')[0]
    t = re.sub(r'[^\w\s\'!?.,$&%:-]', ' ', t, flags=re.ASCII)
    t = re.sub(r'(?i)\b([a-z]*?)([a-z])\2{2,}\b', lambda m: m.group(1) + m.group(2), t)     # doggggg -> dog
    t = re.sub(r'([!?.,])\1+', r'\1', t)
    t = re.sub(r'\s+', ' ', t).strip(' -:._,')
    words = []
    for w in t.split():
        core = re.sub(r"[^A-Za-z']", '', w)
        fix = next((k for k, v in _TYPO.items() if core.lower() in v), None)
        words.append(w.replace(core, fix) if fix and core else w)
    return ' '.join(words)


_PROFANE = re.compile(r"(?i)^(holy|wtf|shi|shii+|shit\w*|bullshit|fuck\w*|bitch\w*|ass|asshole|damn|goddamn|hell|dick\w*|piss\w*|tits?|stfu|cracked|gyat\w*|faty|dih)$")


def decent(title):
    """Title without profanity or slang that would hurt an on-screen label."""
    return ' '.join(w for w in tidy(title).split() if not _PROFANE.match(re.sub(r"[^A-Za-z']", '', w)) and '*' not in w).strip(' -:.,')


def meaningful(title):
    t = decent(title)
    if speech.title_has_slur(t):
        return False
    ws = [w for w in re.findall(r"[A-Za-z']+", t) if len(w) > 1]
    real = [w for w in ws if w.lower() not in _JUNK]
    bad = any(speech._SEXUAL.match(w.lower()) or speech._STRONG.match(w.lower()) or re.match(r'(?i)^(gyat|dih|suck|faty|jerk|ass$)', w) for w in ws)
    return len(real) >= 2 and len(ws) >= 3 and not bad


def titlecase(t):
    out = []
    for i, w in enumerate(t.split()):
        core = re.sub(r'[^A-Za-z]', '', w)
        if core.isupper() and core in ('GTA', 'VI', 'FIFA', 'NBA', 'IRL', 'PC', 'TV', 'USA', 'AI', 'FC'):
            out.append(w)
        elif i and core.lower() in _SMALL:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower() if core.isupper() or core.islower() else w)
    return ' '.join(out)


def best_quote(an, lo=2, hi=7, max_chars=34, near=6.0):
    """A clean, confidently heard line close to the payoff."""
    po = an['moments']['payoff']
    best, bs = '', 0
    for s in sentences(an['speech']['words']):
        ws = s['text'].split()
        txt = re.sub(r'\s+', ' ', s['text']).strip(' ,.-')
        if not lo <= len(ws) <= hi or len(txt) > max_chars or s['p'] < .70 or '*' in txt:
            continue
        cl = [speech.clean(w) for w in ws]
        if any(speech._SLUR.match(c) or speech._STRONG.match(c) or speech._SEXUAL.match(c) or speech._MILD.match(c) for c in cl if c):
            continue
        if len(set(cl)) < len(cl) * .6:
            continue
        d = abs((s['s'] + s['e']) / 2 - po)
        sc = s['p'] + .25 * ('!' in txt or '?' in txt) - .07 * min(d, near) + .03 * len(ws)
        if sc > bs:
            best, bs = txt, sc
    return best


def _pick(seq, key, salt=''):
    h = int(hashlib.sha256((key + salt).encode()).hexdigest()[:8], 16)
    return seq[h % len(seq)]


def mood(an, why):
    if 'laughing' in why:
        return 'laugh'
    if 'big reaction' in why and (an['audio'].get('peak_gap_db') or 0) > 11:
        return 'loud'
    if an['layout']['mode'] != 'split':
        return 'irl'
    return 'calm'


_T = {
    'loud': ['{n} Was NOT Ready for This 😭', '{n} Completely Lost It 💀', 'This Made {n} Lose His Mind 😭', "{n}'s Reaction Says Everything 💀", '{n} Did Not See This Coming 😳'],
    'laugh': ["{n} Couldn't Hold It Together 😂", 'This Broke {n} 😂', "{n} Couldn't Stop Laughing 😂", 'Everybody Lost It After This 😂'],
    'irl': ['Wait for the End With {n} 😳', '{n} Was Not Expecting This 💀', 'Watch {n} Right Here 😭', "{n}'s Face at the End 😂"],
    'calm': ["{n} Couldn't Believe This 😳", '{n} Had to Take a Second 😭', "{n}'s Face Says It All 💀", '{n} Was Speechless 😳'],
}
# Pronouns are never assumed for streamers.
_T['loud'][2] = 'This Sent {n} Over the Edge 😭'
_H = {'loud': ['WAIT FOR THE REACTION 😭', '{N} LOST IT 💀', '{N} WAS NOT READY 😭'], 'laugh': ['{N} IS GONE 😂', 'THIS BROKE {N} 😂'],
      'irl': ['WAIT FOR IT 😳', '{N} DID NOT EXPECT THIS 💀'], 'calm': ['{N} IS SPEECHLESS 😳', "{N}'S FACE 💀"]}


def crowd_titles(src):
    try:
        ct = json.loads(src.get('crowd_titles') or '[]')
    except ValueError:
        ct = []
    return ct or [src.get('title') or '']


def _rejoin(m):
    """Viewer titles lose their apostrophes: "Marlon s", "we re", "don t"."""
    a, e = m.group(1), m.group(2)
    if e == 't' and not a.lower().endswith('n'):
        return m.group(0)
    if e in ('re', 've', 'll') and a.lower() not in ('we', 'you', 'they', 'i', 'he', 'she', 'it', 'who', 'that'):
        return m.group(0)
    return f"{a}'{e}"


def human(t):
    """Sentence case, names and acronyms kept, no shouting."""
    t = re.sub(r':[a-z_]{2,20}:?(?=\s|$)', ' ', re.sub('[\u2018\u2019\u02bc`]', "'", t or ''))
    t = re.sub(r"\b([A-Za-z]+) (s|re|t|ll|ve)\b(?! ?\d)", _rejoin, decent(t)).strip(' !?.,-')
    if sum(c.isupper() for c in t) > .6 * max(1, sum(c.isalpha() for c in t)):
        t = t.lower()
    out = []
    for i, w in enumerate(t.split()):
        core = re.sub(r"[^A-Za-z']", '', w)
        lo = core.lower()
        name = next((n for n in _NAMES if lo in (n.lower(), n.lower() + "'s", n.lower() + 's')), None)
        if name:
            w = w.replace(core, name + ("'s" if lo.endswith("'s") or (lo.endswith('s') and lo != name.lower()) else ''))
        elif lo in ('gta', 'fc', 'nba', 'nfl', 'irl', 'usa', 'ai', 'tv', 'dj'):
            w = w.upper()
        elif i and lo == 'a':
            w = w.replace(core, 'a')
        elif lo == 'i' or lo.startswith("i'"):
            w = w.replace(core, 'I' + core[1:])
        elif i and core.istitle() and len(core) > 1 and sum(x[:1].isupper() for x in t.split()) > .7 * len(t.split()):
            w = w.lower()
        out.append(w)
    t = ' '.join(out)
    return t[:1].upper() + t[1:]


_NAMES = ['Lacy', 'Marlon', 'Ron', 'Silky', 'Jason', 'Adapt', 'Kai', 'Clix', 'Cinna', 'Rakai', 'Agent', 'Duke', 'Tylil', 'Fanum', 'Drake', 'Balenciaga', 'Tokyo', 'Japan', 'Africa', 'Korean', 'Japanese', 'Batman',
          'Spiderman', 'Core', 'Twitch', 'Fortnite', 'Kilimanjaro', 'Laiys', 'Heinz', 'Darla', 'Neon', 'Speed', 'Sakura', 'Gamestop', 'Messi', 'Arsenal', 'Walmart', 'Target', 'Google', 'Lucia']


def describe(src):
    """The best human description of this moment: what viewers themselves called it."""
    best, bs = '', 0
    for i, t in enumerate(crowd_titles(src)):
        if not meaningful(t):
            continue
        d = human(t)
        n, k = len(d), len(d.split())
        if not 12 <= n <= 72 or k < 3 or re.search(r'(?i)(leaked|just chatting|whole chat|grand theft auto|:\w+$|^vid \d|check twitter|clip yt|subscribe|follow me|\bpt\.? ?\d|@\w+)', t):
            continue
        v = min(1, n / 34) + .12 * min(6, k) - .06 * i + .25 * any(x.lower() in d.lower() for x in _NAMES) - .4 * bool(re.search(r'(?i)\b(pt|part) ?\d|stream|day \d', d)) - .3 * (n > 58)
        if v > bs:
            best, bs = d, v
    return best


_VERBS = {'crash': 'crashes', 'fall': 'falls', 'react': 'reacts', 'get': 'gets', 'try': 'tries', 'find': 'finds', 'lose': 'loses', 'win': 'wins', 'run': 'runs', 'scare': 'scares', 'rage': 'rages', 'cook': 'cooks', 'roast': 'roasts', 'meet': 'meets',
          'make': 'makes', 'break': 'breaks', 'jump': 'jumps', 'hit': 'hits', 'scream': 'screams', 'slip': 'slips', 'trip': 'trips', 'drop': 'drops', 'throw': 'throws', 'catch': 'catches', 'ask': 'asks', 'say': 'says', 'call': 'calls',
          'tell': 'tells', 'go': 'goes', 'do': 'does', 'eat': 'eats', 'forget': 'forgets', 'think': 'thinks', 'see': 'sees', 'explain': 'explains', 'talk': 'talks', 'clown': 'clowns', 'prank': 'pranks', 'bully': 'bullies', 'kick': 'kicks',
          'punch': 'punches', 'beat': 'beats', 'dance': 'dances', 'sing': 'sings', 'fail': 'fails', 'cover': 'covers', 'tuck': 'tucks', 'wake': 'wakes', 'steal': 'steals', 'buy': 'buys', 'start': 'starts', 'stop': 'stops', 'take': 'takes', 'give': 'gives'}
_MOOD = re.compile('[\U0001F602\U0001F62D\U0001F480\U0001F923\U0001F633\U0001F631\U0001F92F\U0001F971]')


def natural(t, n=''):
    """Small grammar repairs so a viewer's rough note reads like a title."""
    ws = t.split()
    if len(ws) > 1 and ws[0].rstrip("'s") in _NAMES and ws[1].lower() in _VERBS and not ws[0].endswith("'s"):
        ws[1] = _VERBS[ws[1].lower()]
    t = ' '.join(ws)
    t = re.sub(r'\b([Cc])racks out\b', r"\1rack's out", t)
    t = re.sub(r'\s+([,!?.])', r'\1', t).strip(' ,.-')
    t = re.sub(r"(?i)^(when|pov:?|bro|chat|yo|nah|lol|lmao)\s+(?=\w)", '', t) if len(ws) > 4 else t
    t = re.sub(r'(?i)(\s+(l+m+f*a+o+|l+o+l+|lul|xd|haha\w*|bruh+|bro|fr|ong))+$', '', t) if len(t.split()) > 3 else t
    t = re.sub(r'(?i)\b(that|what|it|he|she|who|there|here|where)s\b(?=\s+(my|a|the|this|that|so|not|going|doing|happening|wrong|up|crazy|wild))', r"\1's", t)
    t = re.sub(r'(?i)\b(dont|cant|wont|didnt|isnt|doesnt|wasnt|aint)\b', lambda m: m.group(1)[:-1] + "'t", t)
    return t[:1].upper() + t[1:]


def mood(src, an):
    """One emoji, the one viewers themselves used most, or what the sound says."""
    c = {}
    for t in crowd_titles(src):
        for e in _MOOD.findall(t or ''):
            c[e] = c.get(e, 0) + 1
    if c:
        return max(c, key=c.get)
    cv = (an or {}).get('curves') or {}
    top = lambda k: max(cv.get(k) or [0])
    if top('scream') > .5:
        return '\U0001F633'
    return '\U0001F62D' if top('laugh') > .35 else '\U0001F480'


def _title_value(t, n):
    plain = re.sub(r'[^\w\s\'"?!]', '', t)
    k, L = len(plain.split()), len(t)
    v = 1.0 - abs(L - 42) / 60 + .35 * (n.lower() in t.lower()) + .12 * any(x.lower() in t.lower() for x in _NAMES if x != n)
    v += .25 * bool(re.search(r"(?i)\b(\w+(es|s)|goes|got|gets|tries|finds|says|is|was|can't|won't)\b", plain)) + .10 * ('?' in t) - .45 * bool(re.search(r'^"|: "', t))
    v -= .5 * (k < 3) + .4 * (L > 70) + .6 * bool(re.search(r'(?i)(not ready for this|funny moments?|\bclip\b|part \d|stream)', t)) + .3 * bool(re.search(r'\d{3,}', t))
    return v


def titles(src, an, why):
    """Title options, best first. Every one comes from what viewers wrote or what was really said."""
    n, g = display(src['streamer']), game_name(src)
    d = natural(describe(src), n) if describe(src) else ''
    q = best_quote(an)
    q = q[:1].upper() + q[1:] if q else ''
    e = mood(src, an)
    cand = []
    named = lambda t: n.lower() in t.lower() or any(re.search(rf"(?i)\b{re.escape(x)}('s)?\b", t) for x in _NAMES[:8])
    whole = lambda t: t.split()[-1].lower().strip('.,') not in _SMALL and t.split()[-1].lower() not in ('his', 'her', 'their', 'my', 'is', 'and', 'with', 'for')
    if d and whole(d):
        cand.append(d if named(d) else f'{d} ({n})')
    else:
        d = ''
    for t in crowd_titles(src):
        if meaningful(t):
            h = natural(human(t), n)
            if 10 <= len(h) <= 68 and whole(h) and not re.search(r'(?i)(leaked|check twitter|subscribe|follow me|@\w+|just chatting|part \d|^are you)', h):
                cand.append(h if named(h) else f'{h} ({n})' if len(h) < 54 else h)
    if q:
        cand += [f'{n}: "{q}"', f'"{q}" - {n}']
        if d and len(d) + len(q) < 62:
            cand.append(f'{d}: "{q}"')
    if not cand:                                  # only when nothing real was said or written about it
        cand.append(f'{n} was not ready for this' + (f' in {g}' if g else ''))
    res, seen = [], set()
    for t in sorted(dict.fromkeys(cand), key=lambda t: -_title_value(t, n) - (.25 if d and t.startswith(d) else 0)):
        t = t[:90].strip()
        key = re.sub(r'\W', '', t.lower())
        if key in seen or speech.title_has_slur(t):
            continue
        seen.add(key)
        res.append(f'{t} {e}')
    return res[:6]


def hook(src, an, text=''):
    """2-5 big words for a cover: a whole thought, never a cut-off one."""
    n = display(src['streamer'])
    q = best_quote(an, 2, 5, 26) or ''
    d = text or (natural(describe(src), n) if describe(src) else '')
    d = re.sub(r'\([^)]*\)', '', d).strip(' "')
    fits = lambda t: 2 <= len(t.split()) <= 5 and len(t) <= 26
    tail = lambda t: t.split()[-1].lower().strip('.,') in _SMALL or t.split()[-1].lower() in ('his', 'her', 'their', 'my', 'is', 'and', 'with', 'for', 'by', 'to', 'as')
    opts = []
    if d:
        names = '|'.join(re.escape(x) for x in dict.fromkeys([n] + _NAMES[:8]))
        short = re.sub(rf"(?i)^({names})('s)?\s+(is\s+|was\s+|just\s+|really\s+)?", '', d).strip()
        short = '' if re.match(r'(?i)(and|or|but|to|of)\b', short) else short
        parts = lambda t: [t] + re.split(r'(?i)\s+(?:and|because|while|after|before|but|so|when|then|who|that|for)\s+', t)[:1] + re.split(r'(?i)\s+(?:in|at|on|with|from|about|by)\s+', t)[:1]
        for t in ([d] if len(d) <= 22 else []) + [x for y in (short, d) if y for x in parts(y)]:
            if t and fits(t) and not tail(t):
                opts.append((1.0 + .2 * (len(t.split()) >= 3), t))
    if q and fits(q) and not re.match(r'(?i)(by the way|i mean|you know|all right|let\'?s go|what\'?s up|nice to meet)', q):
        opts.append((1.15 + .25 * ('?' in q) if not opts else .9 + .45 * ('?' in q), q))
    if opts:
        return max(opts)[1].upper()
    return ' '.join((d or q or n).split()[:4]).upper() if (d or q) and not tail(' '.join((d or q).split()[:4])) else n.upper()


def headline(src, an, why):
    """Short on-screen line, or nothing. A wrong or generic line is worse than none."""
    d = describe(src)
    if d and len(d) <= 46:
        return d
    if d:
        cut = re.split(r'(?i)\s+(?:and then|because|while|after|before|but|so)\s+', d)[0]
        if 12 <= len(cut) <= 46:
            return cut
    return ''


_ANY_EMOJI = re.compile('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\ufe0f\u200d]')


def caption(src, an, why, title=''):
    """The short line that stays on screen: what happens, or the funniest thing said, and one emoji. '' when nothing honest fits."""
    q = best_quote(an, 2, 7, 40)
    plain = re.sub(r'\s*\([^)]*\)\s*$', '', re.sub(r'\s*#\w+', '', _ANY_EMOJI.sub('', title or ''))).strip()
    for line in (headline(src, an, why), f'"{q[:1].upper() + q[1:]}"' if q else '', plain):
        line = _ANY_EMOJI.sub('', line or '').strip()
        line = re.sub(r'(?i)[\s,.!]+(lol|lmao|lmfao|xd|haha+|omg)[.!]*$', '', line).strip()
        if line and len(line) <= 70 and not _JUNK_LINE.search(line):
            return f'{line} {mood(src, an)}'
    return ''


_JUNK_LINE = re.compile(r'(?i)not ready for this|funny moments?|highlight|\bpart\s*\d|\bvod\b|\bstream\b|\bclip\b|\b(?=[0-9a-f]*\d)[0-9a-f]{5,}\b|#\d|\d{4}-\d\d|^\W*$')


def label(src, an, why):
    """2-5 word ranking row."""
    d = describe(src)
    n = display(src['streamer'])
    if d:
        t = re.sub(rf"(?i)^{re.escape(n)}('s)?\s+(just\s+)?", '', d)
        if len(t) <= 27:
            return t[:1].upper() + t[1:]
        w = t.split()
        for k in (5, 4, 3):
            x = ' '.join(w[:k])
            if len(x) <= 27 and w[k - 1].lower() not in _SMALL:
                return x[:1].upper() + x[1:]
    solo = [w for w in re.findall(r"[A-Za-z']+", decent(src.get('title') or '')) if len(w) > 2 and w.lower() not in _JUNK and w.lower() != src['streamer'].lower()]
    if 1 <= len(solo) <= 3 and not speech.title_has_slur(src.get('title')):
        return human(' '.join(solo))
    q = best_quote(an, 2, 4, 22)
    if q:
        return f'"{q[:1].upper() + q[1:]}"'
    return f'{n} in {game_name(src)}' if game_name(src) else f'{n} moment'


def ranking_titles(items):
    who = sorted({it['src']['streamer'] for it in items})
    n = display(who[0]) if len(who) == 1 else ' & '.join(display(w) for w in who[:2])
    games = {game_name(it['src']) for it in items} - {''}
    g = next(iter(games)) if len(games) == 1 else ''
    k = len(items)
    irl = all(it['an']['layout']['mode'] != 'split' for it in items)
    what = f'{g} Reactions' if g and not irl else 'IRL moments' if irl else 'Reactions'
    what = what.lower() if not g else what.replace('Reactions', 'reactions').replace('Moments', 'moments')
    return [f"Ranking {n}'s funniest {what} #shorts", f"{n}'s top {k} {what} this month #shorts", f'{k} {n} {what}, ranked #shorts'], f"Ranking {n}'s funniest {what}"


def hashtags(srcs, extra=''):
    tags = ['#Shorts']
    for s in srcs:
        n = re.sub(r'\W', '', display(s['streamer']))
        for t in (f'#{n}', f'#{n}Clips'):
            if t not in tags:
                tags.append(t)
    for s in srcs:
        g = game_name(s)
        if g:
            t = '#' + re.sub(r'\W', '', g)
            if t not in tags:
                tags.append(t)
    tags += ['#Twitch', '#StreamerClips', '#FunnyMoments']
    for t in re.findall(r'#\w+', extra or ''):
        if t.lower() not in [x.lower() for x in tags]:
            tags.insert(1, t)
    return ' '.join(tags[:12])


def description(srcs, kind, conf, an=None):
    """Short, specific and searchable: what happens, who, where it came from."""
    names = list(dict.fromkeys(display(s['streamer']) for s in srcs))
    camp = conf.get('campaigns') or {}
    s0 = srcs[0]
    g = game_name(s0)
    lines = []
    if kind == 'ranking':
        lines.append(f"The {len(srcs)} best moments from {' and '.join(names)}, ranked.")
    else:
        d = natural(describe(s0), names[0]) if describe(s0) else ''
        q = best_quote(an) if an else ''
        lines.append((d if d else f"A moment from {names[0]}'s stream") + (f' while playing {g}' if g and g.lower() not in d.lower() else '') + '.')
        if q:
            lines.append(f'"{q[:1].upper() + q[1:]}"')
        try:
            when = datetime.fromisoformat((s0.get('created') or '').replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - when).days > 200:
                lines.append(f'Throwback to {when:%B %Y}.')
        except ValueError:
            pass
    lines.append('')
    for login in dict.fromkeys(s['streamer'] for s in srcs):
        if any(str(s.get('url', '')).startswith('http') for s in srcs if s['streamer'] == login):
            lines.append(f'Watch {display(login)} live: https://www.twitch.tv/{login}')
    for s in srcs:
        if str(s.get('url', '')).startswith('http'):
            lines.append(f"Original clip{' by ' + s['creator'] if s.get('creator') else ''}: {s['url']}")
    notes = [str((camp.get(l) or {}).get('note', '')).strip() for l in dict.fromkeys(s['streamer'] for s in srcs)]
    lines += [x for x in notes if x]
    if conf.get('attribution_line'):
        lines.append(conf['attribution_line'])
    extra = ' '.join(str((camp.get(l) or {}).get('hashtags', '')) for l in dict.fromkeys(s['streamer'] for s in srcs))
    tag = ['#' + re.sub(r'\W', '', x).lower() for x in names[:2]] + re.findall(r'#\w+', extra) + ['#shorts', '#twitch', '#funny'] + (['#' + re.sub(r'\W', '', g).lower()] if g else [])
    lines += ['', ' '.join(list(dict.fromkeys(tag))[:7])]
    return '\n'.join(lines)


def tags(srcs):
    out = []
    for s in srcs:
        n = display(s['streamer'])
        out += [n, f'{n} clips', f'{n} twitch', f'{n} funny moments']
        if game_name(s):
            out += [game_name(s), f'{game_name(s)} funny moments']
    out += ['twitch clips', 'streamer clips', 'funny moments', 'FunnyComm']
    res, total = [], 0
    for t in dict.fromkeys(out):
        if total + len(t) + 2 < 440:
            res.append(t)
            total += len(t) + 2
    return res
