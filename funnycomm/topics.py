"""Topics, plain-English video ideas, and automatic idea suggestions."""
from __future__ import annotations
import re, time
from datetime import datetime, timedelta
from . import index, meta

CORE = ['marlon', 'lacy', 'silky', 'jasontheween', 'adapt', 'stableronaldo']
ALIASES = {'marlon': ['marlon', 'mar'], 'lacy': ['lacy'], 'silky': ['silky'], 'jasontheween': ['jasontheween', 'jason', 'ween'], 'adapt': ['adapt'],
           'stableronaldo': ['stableronaldo', 'stable ronaldo', 'ronaldo', 'ron']}

# title: words viewers put in clip titles; talk: words actually said; not: false friends
TOPICS = {
    'rizz': {'name': 'rizzing up girls', 'say': r'rizz\w*|flirt\w*|girls?|dating|dates?|baddies?|huzz',
             'title': r"\b(rizz(es|ed|ing|y|in)?|flirt\w*|gets? (the )?girls?|girls? (love|anywhere|want)|baddies?|huzz|her number|shoots? (his|the) shot|on a date|blind date|speed dat\w+|asks? (her|a girl|girl) out|get(s|ting)? (her|a girl'?s) (number|insta\w*)|trying to pull|pull(s|ed|ing)? (a |the |some )?(girls?|baddies?|huzz|shorty)|talk(s|ing)? to (a |some )?(girl|baddie)s?)\b", 'weak': r'\b(girls?|crush|blush\w*|gf|girlfriend|wif(e|ey)|e-?date|valentine|kiss(es|ed)?|marry|in love|simp\w*|smooth|charming|aishah|sakura|katie ?b|cinna|madi\w*|alyssa|rakia)\b',
             'talk': r"\b(are you single|do you have a boyfriend|you('re| are) (so |really |actually )?(pretty|beautiful|cute|gorgeous|fine|stunning)|what'?s your (name|sign|type|instagram|insta|snap|number)|can i (get|have) your|take you out|"
                     r"go on a date|boyfriend|girlfriend|my type|you('re| are) my type|how old are you|love your (hair|eyes|smile|outfit|accent)|where are you from|kiss|hold my hand|be my)\b",
             'not': r"\b(rizzler|pulls? up|pulled up|card pull|pull(s|ed)? (his|her|the|a|out) ?(leg|gun|guns|iv|card|ipad)?|n word|sneaking|leg|love you|w love|up on)\b|\b(jason|ron|lacy|silky|adapt|marlon) (and|&|x) (jason|ron|lacy|silky|adapt|marlon)\b.*\b(kiss|love|date)|wife follows|michael b", 'female': True},
    'rage': {'name': 'crashing out', 'say': r'rag(e|ing)|crash(es|ing)? ?out|mad|angry|tilted', 'title': r'\b(rag(e|es|ing|ed)|crash(es|ing|ed)? out|freaks? out|loses it|lost it|pissed|slams?|tweak(s|ing|in)?)\b', 'weak': r'\b(mad|angry|tilt\w*|screams?|yell(s|ing)?|throws?)\b', 'talk': r"\b(are you kidding|are you serious|shut up|i'm done|i quit|no way|bullshit)\b", 'not': r'\b(breakfast|breaking news)\b'},
    'fails': {'name': 'biggest fails', 'say': r'fail\w*|falls?|fell|accidents?', 'title': r'\b(fail(s|ed|ing)?|falls?( off| down| over)?|fell|trips? (over|on)|tripped|slips?|slipped|faceplant\w*|wipe ?out|eats shit|oops|spill(s|ed)?|breaks? (his|the))\b', 'weak': r'\b(drops?|dropped|crash(es|ed)|accident\w*|misses|missed|stuck|lost)\b', 'talk': r'', 'not': r'\b(airdrops?|drops? (merch|song|album|video)|falls? in love|crash(es|ing|ed)? out|falls asleep)\b'},
    'pranks': {'name': 'pranks', 'say': r'pranks?|troll\w*|scar(e|ing)', 'title': r'\b(prank\w*|troll\w*|jump ?scares?|wakes? up|water on (him|her)|rage ?bait\w*)\b', 'weak': r'\b(scare[sd]?|scaring|bait\w*|pretend\w*|fooled|got him)\b', 'talk': r"\b(it's a prank|i'm just kidding|got you)\b", 'not': r'\b(card trick)\b'},
    'fights': {'name': 'arguments and roasts', 'say': r'fights?|argu\w+|beef|roast\w*', 'title': r'\b(roast\w*|argu(es|ed|ing|ment)|heated|violat\w+|talking crazy|calls? (him|her|them)? ?out|confront\w*|gets? (checked|pressed|violated|cooked)|beef(ing)?|instant fight|gets? in (a|to a) (fight|argument))\b', 'weak': r'\b(exposed?|flames?|disrespect\w*|diss\w*|fight(s|ing)?|cook(s|ed) (him|her|ron|lacy|marlon|silky|jason|adapt))\b', 'talk': r"\b(shut up|what did you say|you're (fat|short|ugly|bald|broke|weird))\b", 'not': r'\b(cooking|boxing|beef (burger|noodle))\b'},
    'food': {'name': 'food moments', 'say': r'food|eat\w*|cook(ing)?|mukbang', 'title': r'\b(eats?|eating|food|cook(ing|s)?|chef|mukbang|sushi|burger|pizza|spicy|taste\w*|restaurant|noodles?|steak|burn(s|t) the|wasabi|menu)\b', 'talk': r'\b(tastes? like|delicious|spicy|bite|recipe|hungry)\b', 'not': r'\b(cooked|let him cook)\b'},
    'gym': {'name': 'mogging and gym', 'say': r'mog\w*|gym|workouts?|lift\w*', 'title': r'\b(mog\w*|gym|lift\w*|bench\w*|push ?ups?|pull ?ups?|workout|training|shirtless|physique|arm wrestl\w*|aura)\b', 'talk': r'\b(reps?|bench|push ups?|bicep|workout)\b', 'not': r''},
    'sports': {'name': 'sports moments', 'say': r'basketball|soccer|football|boxing|sports?|golf', 'title': r'\b(basketball|dunk\w*|half ?court|soccer|football|goal|boxing|box(es|ing)|fight night|knock(s|ed)? out|golf|baseball|hockey|penalty|1v1|race|sprint|arsenal|psg)\b', 'talk': r'\b(goal|foul|shoot it|pass|game point)\b', 'not': r''},
    'travel': {'name': 'travel chaos', 'say': r'travel|trip|japan|africa|tokyo|abroad|vacation', 'title': r'\b(japan|tokyo|kyoto|osaka|africa|tanzania|kilimanjaro|botswana|safari|lion|korea|china|mexico|cabo|paris|london|dubai|airport|plane|flight|hotel|customs|taxi|vegas|miami|hawaii|sweden)\b', 'talk': r'', 'not': r'\b(in vice city)\b'},
    'scary': {'name': 'scared moments', 'say': r'scary|scared|horror|haunted', 'title': r'\b(scared|jump ?scare\w*|horror|haunted|ghost|creepy|ran away|paranormal)\b', 'weak': r'\b(scar(y|es)|screams?)\b', 'talk': r'', 'not': r'\b(freak$)\b'},
    'celebs': {'name': 'meeting famous people', 'say': r'celebs?|celebrit\w+|famous|meets?', 'title': r'\b(kai cenat|ishowspeed|speed|clix|tylil|duke dennis|fanum|agent|drake|mrbeast|mr beast|rakai|cinna|valkyrae|xqc|adin|neon|sketch|messi|ronaldo|lebron|nfl|nba|interview\w*|collab|meets?|met)\b', 'talk': r'', 'not': r'\b(stable ?ronaldo|speed ?run|meets? up)\b'},
    'dance': {'name': 'singing and dancing', 'say': r'danc\w+|sing\w*|karaoke', 'title': r'\b(danc\w+|sing(s|ing)?|karaoke|freestyle|rap(s|ping)?|twerk\w*|moonwalk|hits? the)\b', 'talk': r'', 'not': r'\b(single)\b', 'music': True},
    'wholesome': {'name': 'wholesome moments', 'say': r'wholesome|sweet|cute', 'title': r'\b(wholesome|sweet|cute|hug\w*|surpris\w+|gift\w*|donat\w+|helps?|dog|puppy|baby|kid|fan (meet|interaction)|proud)\b', 'talk': r'', 'not': r'\b(gifted subs?)\b'},
    'gta': {'name': 'GTA 6', 'say': r'gta ?(6|vi)?|grand theft', 'title': r'\b(gta|vice city|rockstar)\b', 'talk': r'', 'not': r'', 'game': 'Grand Theft Auto VI'},
    'house': {'name': 'house moments', 'say': r'house', 'title': r'\b(house|room ?mate|kitchen|bedroom|couch|fridge|pool|chores|wakes? up|sleep\w*|roommate|basement|door|toilet|bed|blanket|tucks?|naps?|dinner|lunch|breakfast|eating|food|mattress|couch|garage|backyard|shower|bathroom|chef|cook(s|ing)|thanksgiving)\b', 'talk': r'', 'not': r''},
}
for t in TOPICS.values():
    for k in ('title', 'weak', 'talk', 'not'):
        t['_' + k] = re.compile('(?i)' + t[k]) if t.get(k) else None
STOP = set('the a an of in on to for and or with best top funniest funny moments moment clips clip video compilation last past this week weeks month months day days all time ever most at his her their core house boys up from make me new about show when who is are every being'.split())


def match_text(key, titles, talk=''):
    """0..1 how clearly viewers' titles / speech say this moment is about the topic."""
    t = TOPICS.get(key)
    if not t:
        return 0.0
    hit = 0.0
    for i, x in enumerate(titles):
        place = 1.0 if i < 3 else .85
        m = t['_title'].search(x) if t['_title'] else None
        if m and not (t['_not'] and t['_not'].search(x) and not re.search(r'(?i)\b(flirt\w*|w rizz|l rizz|rizz(es|ed|ing))\b', x)):
            hit = max(hit, place)
        elif t.get('_weak') and t['_weak'].search(x) and not (t['_not'] and t['_not'].search(x)):
            hit = max(hit, .55 * place)
    n = len(t['_talk'].findall(talk)) if t.get('_talk') and talk else 0
    return min(1.0, max(hit, min(.9, .30 * n)) + (.15 if hit and n else 0))


def match_words(words, titles, talk=''):
    """Free keywords for custom topics."""
    if not words:
        return 0.0
    tx, best = ' | '.join(titles).lower(), 0
    for w in words:
        stem = re.escape(w[:-1] if len(w) > 4 and w.endswith('s') else w)
        if re.search(rf'\b{stem}', tx):
            best += 1
        elif talk and re.search(rf'\b{stem}', talk.lower()):
            best += .55
    return min(1.0, best / max(1, min(2, len(words))))


# ---------------------------------------------------------------------- ideas
def groups(conf):
    g = {'core': CORE}
    g.update(conf.get('groups') or {})
    return g


def parse(text, conf, now=None):
    """'Marlon rizzing up girls' -> a search plan. Deterministic and explainable."""
    now = now or time.time()
    raw = re.sub(r'\s+', ' ', text or '').strip().strip('"\'')
    low = raw.lower().replace('teh ', 'the ')
    who, group = [], None
    for g, members in groups(conf).items():
        if re.search(rf'\b{re.escape(g)}\b( ?(house|boys|crew|guys))?', low):
            group, who = g, list(members)
    known = {**{s: [s] for s in conf.get('streamers', [])}, **ALIASES}
    named = [s for s, names in known.items() if any(re.search(rf"\b{re.escape(n)}('s)?\b", low) for n in names if len(n) > 2)]
    if named:
        who = named if not group else [w for w in who if w in named] or who
    if not who:
        who = list(conf.get('streamers') or ['lacy', 'marlon'])
    # --- dates
    days, label = None, ''
    m = re.search(r'(?:last|past|this)\s+(\d+)?\s*(day|week|month)s?', low)
    if m:
        days = int(m.group(1) or 1) * {'day': 1, 'week': 7, 'month': 30}[m.group(2)]
        label = {7: 'this week', 30: 'this month', 1: 'today'}.get(days, f'last {days} days')
    elif re.search(r'\b(today|yesterday)\b', low):
        days, label = 2, 'today'
    elif re.search(r'\b(all time|ever|of 2026|this year)\b', low):
        days, label = index.KEEP_DAYS, ''
    month = re.search(r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\b', low)
    t1 = now
    if month:
        mi = datetime.strptime(month.group(1).title(), '%B').month
        y = datetime.fromtimestamp(now).year - (mi > datetime.fromtimestamp(now).month)
        a = datetime(y, mi, 1)
        z = datetime(y + (mi == 12), mi % 12 + 1, 1)
        t0, t1, label = a.timestamp(), min(now, z.timestamp()), month.group(1).title()
        days = (t1 - t0) / 86400
    # --- topic
    topic = None
    for k, t in TOPICS.items():
        if k == 'house' and group:
            continue
        if re.search(rf'(?i)\b({t["say"]})\b', re.sub('|'.join(re.escape(n) for ns in known.values() for n in ns if len(n) > 2) or 'zzz', ' ', low)):
            topic = k
            break
    recap = bool(re.search(r'\b(recap|week in|month in|days? in|what happened|diary|vlog)\b', low))
    best = bool(re.search(r'\b(best|top|funniest|greatest|craziest|wildest)\b', low))
    names = {n for ns in known.values() for n in ns} | set(groups(conf))
    words = [w for w in re.findall(r"[a-z0-9']{3,}", low) if w not in STOP and w not in names and not re.match(r'^\d+$', w)]
    kind = 'recap' if recap else 'topic' if topic or (words and not best) else 'best'
    if kind == 'topic' and not topic and not words:
        kind = 'best'
    if days is None:
        days = 180 if kind == 'topic' else 7 if kind == 'recap' else 60
    if not month:
        t0 = now - days * 86400
    mins = re.search(r'(\d+)\s*(?:min|minutes)\b', low)
    return {'text': raw, 'kind': kind, 'streamers': who, 'group': group, 'topic': topic, 'words': [] if topic else words if kind == 'topic' else [], 't0': t0, 't1': t1, 'days': round(days),
            'label': label, 'also': [g for g in (groups(conf).get('core') or []) if index.kv_get('index:' + g)] if len(who) == 1 and who[0] in CORE else [], 'minutes': min(25, max(3, int(mins.group(1)))) if mins else 10, 'title': idea_title(raw, kind, who, group, topic, label)}


def _cap(s):
    small = {'a', 'an', 'the', 'of', 'in', 'on', 'to', 'for', 'and', 'up'}
    w = s.split()
    return ' '.join(x if x.isupper() and len(x) < 5 else x.lower() if i and x.lower() in small else x[:1].upper() + x[1:] for i, x in enumerate(w))


def idea_title(raw, kind, who, group, topic, label):
    n = 'the Core House' if group == 'core' else ' & '.join(meta.display(w) for w in who[:2])
    if kind == 'recap':
        return f'A Week in {n}' if 'week' in label else f'What Happened in {n} {label.title()}'.strip()
    if kind == 'best':
        return f'Best of {n}' + (f' {label.title()}' if label else '')
    return _cap(re.sub(r'\bteh\b', 'the', raw))


def search(spec, limit=400):
    """Ranked candidate moments with relevance 0..1 (metadata only)."""
    out = []
    solo = spec['streamers'][0] if len(spec['streamers']) == 1 else None
    also = [c for c in spec.get('also') or [] if c not in spec['streamers']]
    name = re.compile(r'(?i)\b(' + '|'.join(re.escape(n) for n in ALIASES.get(solo, [solo or '']) if len(n) > 3) + r')\b') if solo else None
    others = '|'.join(re.escape(n) for k, ns in ALIASES.items() if k != solo for n in ns if len(n) > 2)
    other_rizz = re.compile(rf"(?i)\b({others})('s)? (is |was )?(rizz|flirt)")
    for m in index.moments(spec['streamers'] + also, spec['t0'], spec['t1'] + 1):
        if m['streamer'] in also and not (name and any(name.search(t) for t in m['titles'])):
            continue
        if m['flags']['bad'] or not 5 <= m['best']['seconds'] <= 62:
            continue
        rel = 1.0
        if spec['kind'] == 'topic':
            rel = match_text(spec['topic'], m['titles']) if spec['topic'] else match_words(spec['words'], m['titles'])
            t = TOPICS.get(spec['topic'] or '', {})
            if t.get('game') and m['game'] == t['game']:
                rel = max(rel, .8)
            if rel <= 0:
                # unlabelled in-person moments may still be on topic; they are checked after analysis
                rel = .12 if t.get('female') and m['game'] in ('Just Chatting', '', 'IRL', 'Travel & Outdoors') and m['people'] >= 3 else 0
            elif solo and spec['topic'] == 'rizz' and any(other_rizz.search(x) for x in m['titles']):
                rel = .2               # somebody else is doing it
        if rel <= 0:
            continue
        m['rel'] = rel
        m['rank'] = (m['score'] * (.45 + .55 * rel) + (.55 * rel if spec['kind'] == 'topic' else 0)) * (.93 if m['streamer'] in also else 1)
        out.append(m)
    out.sort(key=lambda m: -m['rank'])
    return out[:limit]


def suggest(conf, now=None):
    """Video ideas the available clips can actually support, best first."""
    now = now or time.time()
    ideas = []
    cores = [s for s in groups(conf)['core'] if index.kv_get('index:' + s)]
    solo = [s for s in dict.fromkeys(list(conf.get('streamers', [])) + cores) if index.kv_get('index:' + s)]

    def add(text, why_fmt, need=14):
        sp = parse(text, conf, now)
        ms = search(sp, 160)
        strong = [m for m in ms if m.get('rel', 1) >= .5 and m['score'] > .28]
        secs = sum(min(45, m['best']['seconds']) for m in strong)
        if len(strong) < need:
            return
        heat = sum(m['score'] for m in strong[:20]) / 20
        ideas.append({'text': text, 'title': sp['title'], 'moments': len(strong), 'minutes': round(min(secs * .8 / 60, 30), 1), 'heat': round(heat, 3), 'why': why_fmt.format(n=len(strong), p=max(m['people'] for m in strong)),
                      'spec': sp, 'fresh': sum(1 for m in strong if now - m['ts'] < 8 * 86400)})
    for s in solo:
        n = meta.display(s)
        add(f'Best of {n} last week', '{n} strong moments in 7 days', 10)
        add(f'Best of {n} this month', '{n} strong moments in 30 days; top one clipped by {p} people')
        add(f'Best of {n}', '{n} strong moments in 60 days; top one clipped by {p} people')
        for k, t in TOPICS.items():
            if k in ('house', 'gta', 'dance'):
                continue
            add(f'{n} {t["name"]}', '{n} on-topic moments in 180 days')
    if len(cores) >= 2:
        add('Last week in the Core house', '{n} strong moments across the house this week', 14)
        add('Best of the Core house this month', '{n} strong moments across the house this month')
        for k in ('pranks', 'fights', 'fails', 'food', 'travel', 'rage', 'scary'):
            add(f'Core house {TOPICS[k]["name"]}', '{n} on-topic moments across the house')
    for i in ideas:
        i['raw'] = i['heat'] * (.75 + .25 * min(1, i['minutes'] / 10)) * (.8 + .2 * min(1, i['fresh'] / 12))
    top = max((i['raw'] for i in ideas), default=1)
    for i in ideas:
        i['potential'] = round(100 * i['raw'] / top)
    ideas.sort(key=lambda i: -i['potential'])
    # One idea per (people, angle) family first, so the list is not ten versions of the same video.
    seen, first, rest = {}, [], []
    for i in ideas:
        k = (tuple(i['spec']['streamers']), i['spec']['topic'] or i['spec']['kind'])
        seen[k] = seen.get(k, 0) + 1
        (first if seen[k] <= 1 else rest).append(i)
    return first + rest
