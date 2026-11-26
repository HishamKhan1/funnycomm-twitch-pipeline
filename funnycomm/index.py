"""Wide clip index and crowd-sourced moments.

Every public clip's details (no video) for up to 180 days are kept locally.
When several different viewers clip the same seconds of a stream, that is a far
better sign of a real moment than one clip with a lot of views.
"""
from __future__ import annotations
import json, math, re, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import pipeline_core as core
from . import media, speech

KEEP_DAYS = 180


def ts_of(created):
    try:
        return datetime.fromisoformat((created or '').replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


def kv_get(k, default=None):
    with core.db() as con:
        r = con.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
    return json.loads(r['v']) if r else default


def kv_set(k, v):
    with core.db() as con:
        con.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, json.dumps(v)))


def update(tw, logins, days=KEEP_DAYS, notify=lambda m: None, progress=lambda a, b, m='': None, force=False):
    """Refresh the index. First run covers everything; later runs only recent weeks."""
    total = 0
    now = datetime.now(timezone.utc)
    for li, login in enumerate(logins):
        media.check_cancel()
        state = kv_get('index:' + login, {})
        full = force or time.time() - state.get('full', 0) > 10 * 86400 or state.get('days', 0) < days
        if not full and time.time() - state.get('last', 0) < 1800:
            continue
        span = days if full else 21
        try:
            uid = tw.broadcaster(login)['id']
        except RuntimeError as e:
            notify(f'{login}: {e}')
            continue
        rows, gids, weeks = {}, {}, math.ceil(span / 7)
        for wk in range(weeks):
            media.check_cancel()
            progress(li + wk / weeks, len(logins), f'Indexing {login}: week {wk + 1} of {weeks}')
            e = now - timedelta(days=7 * wk)
            s = e - timedelta(days=7)
            p = {'broadcaster_id': uid, 'first': 100, 'started_at': s.strftime('%Y-%m-%dT%H:%M:%SZ'), 'ended_at': e.strftime('%Y-%m-%dT%H:%M:%SZ')}
            for _ in range(4):
                r = tw.get('clips', p)
                for c in r.get('data', []):
                    rows[c['id']] = c
                    gids[c['id']] = c.get('game_id')
                cur = r.get('pagination', {}).get('cursor')
                if not cur:
                    break
                p['after'] = cur
        tw.game_names(gids.values())
        data = [('clip:' + c['id'], login, uid, c['url'], c.get('title') or '', int(c.get('view_count', 0)), float(c.get('duration', 0)), c.get('created_at', ''),
                 ts_of(c.get('created_at', '')), tw.games.get(gids[c['id']], ''), c.get('video_id') or None, c.get('vod_offset'), c.get('creator_name', ''), c.get('thumbnail_url', ''))
                for c in rows.values()]
        with core.db() as con:
            con.executemany('''INSERT INTO clip_index(id,streamer,broadcaster_id,url,title,views,seconds,created,ts,game,video_id,vod_offset,creator,thumb_url)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET views=excluded.views,title=excluded.title,game=excluded.game,
                video_id=COALESCE(excluded.video_id,video_id),vod_offset=COALESCE(excluded.vod_offset,vod_offset)''', data)
        kv_set('index:' + login, {**state, 'full': time.time() if full else state.get('full', 0), 'days': max(days, state.get('days', 0)) if full else state.get('days', 0), 'last': time.time()})
        total += len(data)
        notify(f'{login}: {len(data):,} clips indexed ({span} days)')
    return total


def backfill(tw, logins, notify=lambda m: None, progress=lambda a, b, m='': None, max_years=11):
    """One time per channel: the most-watched clips of every older month, so content never runs out."""
    total = 0
    for li, login in enumerate(logins):
        state = kv_get('index:' + login, {})
        if state.get('alltime'):
            continue
        try:
            uid = tw.broadcaster(login)['id']
        except RuntimeError as e:
            notify(f'{login}: {e}')
            continue
        end = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
        empty, got, rows, gids, steps = 0, 0, {}, {}, int(max_years * 12.2)
        for k in range(steps):
            media.check_cancel()
            progress(li + k / steps, len(logins), f'Finding older clips of {login}: {end:%B %Y}')
            start = end - timedelta(days=30)
            p = {'broadcaster_id': uid, 'first': 100, 'started_at': start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'ended_at': end.strftime('%Y-%m-%dT%H:%M:%SZ')}
            found = 0
            for _ in range(3):
                r = tw.get('clips', p)
                for c in r.get('data', []):
                    rows[c['id']] = c
                    gids[c['id']] = c.get('game_id')
                    found += 1
                cur = r.get('pagination', {}).get('cursor')
                if not cur:
                    break
                p['after'] = cur
            got += found
            empty = 0 if found else empty + 1
            end = start
            if empty >= 14:
                break
        tw.game_names(gids.values())
        data = [('clip:' + c['id'], login, uid, c['url'], c.get('title') or '', int(c.get('view_count', 0)), float(c.get('duration', 0)), c.get('created_at', ''),
                 ts_of(c.get('created_at', '')), tw.games.get(gids[c['id']], ''), c.get('video_id') or None, c.get('vod_offset'), c.get('creator_name', ''), c.get('thumbnail_url', '')) for c in rows.values()]
        with core.db() as con:
            con.executemany("""INSERT INTO clip_index(id,streamer,broadcaster_id,url,title,views,seconds,created,ts,game,video_id,vod_offset,creator,thumb_url)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET views=excluded.views""", data)
        state = kv_get('index:' + login, {})
        kv_set('index:' + login, {**state, 'alltime': time.time(), 'last': state.get('last', 0) + .001})
        total += len(data)
        first = min((x['created_at'][:7] for x in rows.values()), default='-')
        notify(f'{login}: {len(data):,} older clips added, back to {first}')
    return total


def clips(logins, t0, t1):
    q = ','.join('?' * len(logins))
    with core.db() as con:
        rows = [dict(r) for r in con.execute(f'SELECT * FROM clip_index WHERE streamer IN ({q}) AND ts>=? AND ts<?', (*logins, t0, t1))]
    for r in rows:                                  # Twitch gives the second the clip ENDS at; here it means where it starts
        if r['vod_offset'] is not None:
            r['vod_offset'] = max(0.0, r['vod_offset'] - (r['seconds'] or 0))
    return rows


# -------------------------------------------------------------------- moments
def _group(xs, start, end, join):
    out, cur, last = [], [], -1e18
    for x in sorted(xs, key=start):
        if cur and start(x) < last + join:
            cur.append(x)
            last = max(last, end(x))
        else:
            if cur:
                out.append(cur)
            cur, last = [x], end(x)
    if cur:
        out.append(cur)
    return out


_cache = {}


def moments(logins, t0, t1):
    """Cluster clips of the same stream seconds. Most-clipped first."""
    res = []
    for who in logins:
        stamp = (kv_get('index:' + who) or {}).get('last')
        if _cache.get(who, (None,))[0] != stamp:
            _cache[who] = (stamp, _cluster(who))
        res += [dict(m) for m in _cache[who][1] if t0 <= m['ts'] < t1]
    res.sort(key=lambda m: -m['score'])
    return res


def _cluster(who):
    res = []
    for who, xs in ((who, clips([who], 0, time.time() + 9e4)),):
        # Twitch pre-fills a clip with the stream's own title; that says nothing about the moment.
        freq = defaultdict(int)
        for x in xs:
            freq[x['title'].strip().lower()] += 1
        for x in xs:
            t = x['title'].strip()
            x['default_title'] = (freq[t.lower()] >= 4 and len(t) > 22) or bool(re.search(r'![a-z]{3,}', t)) and len(t) > 30
        vod, loose = defaultdict(list), []
        for x in xs:
            (vod[x['video_id']].append(x) if x['video_id'] and x['vod_offset'] is not None else loose.append(x))
        groups = []
        for v in vod.values():
            groups += [('vod', g) for g in _group(v, lambda x: x['vod_offset'], lambda x: x['vod_offset'] + x['seconds'], -4)]
        groups += [('time', g) for g in _group(loose, lambda x: x['ts'], lambda x: x['ts'], 70)]
        for how, g in groups:
            res += [_moment(how, p) for p in _split_long(how, g)]
    return res


def _split_long(how, g):
    """A chain of overlaps can run for minutes; break it at the thinnest point."""
    if how != 'vod' or len(g) < 4:
        return [g]
    a, b = min(x['vod_offset'] for x in g), max(x['vod_offset'] + x['seconds'] for x in g)
    if b - a <= 110:
        return [g]
    n = int(b - a) + 1
    d = [0] * n
    for x in g:
        for i in range(int(x['vod_offset'] - a), min(n, int(x['vod_offset'] + x['seconds'] - a))):
            d[i] += 1
    lo, hi = int(n * .25), int(n * .75)
    k = min(range(lo, hi), key=lambda i: d[i])
    left = [x for x in g if x['vod_offset'] + x['seconds'] / 2 - a <= k]
    right = [x for x in g if x not in left]
    if not left or not right:
        return [g]
    return _split_long(how, left) + _split_long(how, right)


_FUN = re.compile(r"(?i)\b(l+m+f*a+o+|l+o+l+|lmf+ao+|ha(ha)+|dead|dies|died laughing|funny|hilarious|crash(es|ing|ed)? out|cook(ed|s)?|rage\w*|roast\w*|prank\w*|trip(s|ped|pin)?|fall(s|ing)?|fell|"
                  r"caught|exposed|jump ?scare\w*|scared|scream\w*|fail\w*|awkward|cring\w*|wild|crazy|insane|tweak\w*|trippin|bro thought|bait\w*|mall cop|security|fight|argu\w+|yell\w*|lost it|rizz\w*|troll\w*|confus\w+|"
                  r"accident\w*|forgot|wrong|stuck|panic\w*|caught lacking|gets? (cooked|roasted|pranked|violated)|no way|bruh+|goofy|geeked|aura)\b|[😂🤣💀😭☠]")
_SAD = re.compile(r'(?i)\b(rip|funeral|passed away|memorial|crying|cries|sad|emotional|tears|apolog\w+|depress\w+|hospital|swat(ted|ting)?|robbed|shooting|lawsuit|sue|suing|award speech|grateful|proud|injur\w+|iv drip)\b')
_SEX = re.compile(r'(?i)\b(gyat+\w*|naked|nude|boner|penis|dick|dih|d!ck|cock|pussy|tits|boobs?|ass(es)?|strip(per|club)?|horny|sex\w*|goon\w*|freak|lucia|mandy|shower|thong|lap ?dance|moan\w*|onlyfans|print|bbl|suck\w*|nut|cum|faty|fatty)\b')
_SEX2 = re.compile(r'(?i)(a\$\$|a\*\*|\bleaked?\b|check twitter|\bnsfw\b|\bflash(es|ed)\b|squirt\w*|see his banana|moist|cheeks|\bclapp(ing|ed)|\bpackin\b|jdab|booba|freaky|daddy|\bho(e|es)?\b)')
_BAD = re.compile(r'(?i)\b(n[ -]?word|hard r|slur|racist|nazi|hitler|kkk|terroris\w+|rape\w*|pedo\w*|minor|suicide|kys|overdose)\b')


def text_flags(titles):
    t = ' | '.join(titles)
    return {'fun': len(_FUN.findall(t)), 'sad': bool(_SAD.search(t)), 'sex': len(_SEX.findall(t)) + 2 * len(_SEX2.findall(t)), 'bad': bool(_BAD.search(t)) or speech.title_has_slur(t)}


def _moment(how, g):
    people = len({x['creator'] for x in g})
    views = sum(x['views'] for x in g)
    titles = list(dict.fromkeys(x['title'].strip() for x in sorted(g, key=lambda x: -x['views']) if x['title'].strip() and not x.get('default_title')))
    fl = text_flags(titles)
    if how == 'vod':
        a, b = min(x['vod_offset'] for x in g), max(x['vod_offset'] + x['seconds'] for x in g)

        def cover(x):
            mid0, mid1 = x['vod_offset'], x['vod_offset'] + x['seconds']
            return sum(max(0, min(mid1, y['vod_offset'] + y['seconds']) - max(mid0, y['vod_offset'])) for y in g) / max(1, len(g) * x['seconds'])
    else:
        a = b = None

        def cover(x):
            return .5

    def fit(x):
        s = x['seconds']
        return 1.0 if 12 <= s <= 45 else .75 if 8 <= s <= 60 else .45
    best = max(g, key=lambda x: .8 * cover(x) + .55 * math.log1p(x['views']) / 8 + .5 * fit(x) + .12 * min(1, len(x['title']) / 24))
    age = max(1.0, (time.time() - min(x['ts'] for x in g)) / 86400)
    s = (.40 * min(1.2, math.log1p(people) / math.log(11)) + .24 * min(1.2, math.log1p(views) / math.log(30000)) + .10 * min(1, math.log1p(views / age) / math.log(900))
         + .20 * min(1, fl['fun'] / 2) + .06 * min(1, len(g) / 8))
    s -= .30 * min(1, fl['sex'] / 2) + .22 * fl['sad'] + (.9 if fl['bad'] else 0)
    if best['game'] in ('Grand Theft Auto VI',) and not fl['fun']:
        s -= .10
    return {'key': best['id'], 'streamer': best['streamer'], 'ts': min(x['ts'] for x in g), 'people': people, 'views': views, 'n': len(g), 'titles': titles[:8], 'game': best['game'],
            'best': best, 'clips': sorted(g, key=lambda x: -x['views']), 'span': (a, b) if how == 'vod' else None, 'vod': best['video_id'] if how == 'vod' else None, 'flags': fl, 'score': round(s, 4)}


def moment_of(clip_id, logins=None):
    """The crowd moment a given clip belongs to, or None."""
    with core.db() as con:
        r = con.execute('SELECT * FROM clip_index WHERE id=?', (clip_id,)).fetchone()
    if not r:
        return None
    for m in moments([r['streamer']], r['ts'] - 2 * 86400, r['ts'] + 2 * 86400):
        if any(c['id'] == clip_id for c in m['clips']):
            return m
    return None


def scene_parts(m, max_seconds=100):
    """Clips from one VOD moment that chain into a longer continuous scene (in order)."""
    if not m.get('span') or len(m['clips']) < 2:
        return [m['best']]
    b = m['best']
    chain, end, start = [b], b['vod_offset'] + b['seconds'], b['vod_offset']
    pool = [c for c in m['clips'] if c['id'] != b['id'] and c['seconds'] >= 8]
    for _ in range(3):
        nxt = [c for c in pool if c['vod_offset'] < end - 3 and c['vod_offset'] + c['seconds'] > end + 9 and c['vod_offset'] + c['seconds'] - start <= max_seconds]
        if nxt:
            c = max(nxt, key=lambda c: (c['vod_offset'] + c['seconds'] - end, c['views']))
            chain.append(c)
            end = c['vod_offset'] + c['seconds']
            pool.remove(c)
            continue
        prv = [c for c in pool if c['vod_offset'] + c['seconds'] > start + 3 and c['vod_offset'] < start - 9 and end - c['vod_offset'] <= max_seconds]
        if prv:
            c = max(prv, key=lambda c: (start - c['vod_offset'], c['views']))
            chain.insert(0, c)
            start = c['vod_offset']
            pool.remove(c)
            continue
        break
    return chain


def to_source(c, m=None):
    """clip_index row -> source record."""
    return {'id': c['id'], 'streamer': c['streamer'], 'source_kind': 'clip', 'url': c['url'], 'title': c['title'] or 'Twitch clip', 'views': c['views'], 'seconds': c['seconds'], 'created': c['created'],
            'broadcaster_id': c['broadcaster_id'], 'video_id': c['video_id'], 'vod_offset': c['vod_offset'] + (c['seconds'] or 0) if c['vod_offset'] is not None else None, 'game': c['game'], 'creator': c['creator'], 'thumb_url': c['thumb_url'],
            'people': m['people'] if m else 1, 'crowd_titles': json.dumps(m['titles'], ensure_ascii=False) if m else None}


MIX = {'mix': (.45, .30, .25), 'new': (.80, .20, 0.0), 'classic': (0.0, .20, .80)}


def unseen(logins, t0=0, t1=None):
    """Every moment not yet used, skipped or rejected, best first."""
    with core.db() as con:
        done = {r['id'] for r in con.execute("SELECT id FROM source WHERE state IN ('downloaded','used','existing','skipped') OR COALESCE(tries,0)>=4")} | core.skip_ids()
    out = []
    for m in moments(logins, t0, t1 or time.time() + 3600):
        f = m['flags']
        if f['bad'] or f['sad'] or f['sex'] >= 2 or not 6 <= m['best']['seconds'] <= 60 or any(c['id'] in done for c in m['clips']):
            continue
        out.append(m)
    return out


def pick_mix(logins, want, mode='mix', notify=lambda m: None):
    """A balanced shopping list: fresh clips, recent ones and all-time classics, shared between streamers."""
    now = time.time()
    part = MIX.get(mode, MIX['mix'])
    lanes = {}
    for who in logins:
        ms = unseen([who])
        age = lambda m: (now - m['ts']) / 86400
        # A game playthrough dates quickly and is judged by its own rules; real-life moments last.
        from . import meta
        key = lambda m: m['score'] - (.12 if m['game'] and m['game'] not in ('Just Chatting', 'IRL') else 0) + .10 * any(meta.meaningful(t) for t in m['titles'][:4]) - .10 * (m['people'] < 2 and m['views'] < 60)
        lanes[who] = [sorted((m for m in ms if a <= age(m) < z), key=key, reverse=True) for a, z in ((-1, 10), (10, KEEP_DAYS), (KEEP_DAYS, 1e9))]
        notify(f'{who}: {len(lanes[who][0]):,} new, {len(lanes[who][1]):,} recent and {len(lanes[who][2]):,} all-time moments not used yet')
    out, quota = [], [0.0, 0.0, 0.0]
    while len(out) < want and any(any(l) for l in lanes.values()):
        before = len(out)
        for who in logins:
            if len(out) >= want:
                break
            order = sorted(range(3), key=lambda k: quota[k] / part[k] if part[k] else 1e9 + k)
            for k in order:
                if lanes[who][k] and (part[k] or not any(lanes[who][j] for j in range(3) if part[j])):
                    out.append(lanes[who][k].pop(0))
                    quota[k] += 1
                    break
        if len(out) == before:
            break
    return out


def pick_for_shorts(logins, days, per_streamer, notify=lambda m: None):
    """Best unseen crowd moments for the Shorts queue, spread across weeks."""
    now = time.time()
    with core.db() as con:
        done = {r['id'] for r in con.execute("SELECT id FROM source WHERE state IN ('downloaded','used','existing','skipped')")} | core.skip_ids()
    out = []
    for who in logins:
        ms = [m for m in moments([who], now - days * 86400, now + 3600) if not m['flags']['bad'] and not any(c['id'] in done for c in m['clips']) and 5 <= m['best']['seconds'] <= 60]
        lanes = defaultdict(list)
        for m in ms:
            lanes[int((now - m['ts']) // (7 * 86400))].append(m)
        took = []
        while len(took) < per_streamer and any(lanes.values()):
            for k in sorted(lanes):
                if lanes[k] and len(took) < per_streamer:
                    took.append(lanes[k].pop(0))
        out += took
        notify(f'{who}: {len(ms):,} unseen moments, kept the best {len(took)} (top one clipped by {max([m["people"] for m in took], default=0)} people)')
    return out
