"""16:9 YouTube videos from crowd-picked moments: gather, choose, order, cut, render, package."""
from __future__ import annotations
import hashlib, json, os, random, re, shutil, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import pipeline_core as core
import pipeline_render as pr
from . import analyze, audio, ears, gfx, index, media, meta, render, safety, speech, story, topics, vision

W, H, FPS = 1920, 1080, 60
CACHE = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'FunnyComm' / 'pieces'
_MINOR = re.compile(r"(?i)\b(i'?m|i am|she'?s|he'?s|you'?re)\s+(only\s+)?(1[0-7]|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)\b(?!\s*(minutes|percent|bucks|dollars|subs|k\b))|\b(underage|a minor|middle school|high school(er)?|jailbait)\b")


_CRUDE = re.compile(r"(?i)\b(pussy|dick|cock|clap(ping)? (cheeks|those)|cheeks|suck (my|his|on)|fuck(ing)? (on )?(her|these women|that bitch|lucia|mandy)|nut(ted|ting)?|horny|jerk(ing)? off|jerk my|titties|boobs?|punished|bad boy|moan\w*|erection|boner|cum(ming)?)\b")
# One of these is enough to leave a scene out of an automatic video.
_CRUDE_HARD = re.compile(r"(?i)\b(back ?shots?|suck (your|his|my|her|this|that) (dick|cock|balls)|eat(ing|s)? (that |some |yo |your )?ass\b|eat (me|her|you) out|ate (her|me|him) out|(getting|got) plowed|sex with|gave? (me|her|him) head|(some|want) neck|rape\w*)")
_PRIVATE = re.compile(r"(?i)\b(stalk\w*|messag\w+ my|my (whole |entire )?family|parasocial|br(oke|oken) up|rumou?rs?|unfollow\w*|lawyers?|lawsuit|allegations?|restraining|address(es|ed|ing)? (the|this|that) (situation|rumou?rs?|drama)|clears? rumou?rs?|weird fans?|cheated on|custody)\b")
_ETHNIC = re.compile(r"(?i)\b(chinese|asians?|mexicans?|indians?|koreans?|japanese|kung fu|bruce lee|chopsticks?|africans?|arabs?|muslims?|jews?|jewish|immigrants?)\b")


def unfit(an, titles=()):
    """Why a safe clip still does not belong in an automatic compilation ('' when fine)."""
    t = talk_of(an)
    if _CRUDE_HARD.search(t) or len(_CRUDE.findall(t)) >= 2:
        return 'crude sexual talk'
    if len(_PRIVATE.findall(t + ' ' + ' '.join(titles or []))) >= 2:
        return 'private drama, not comedy'
    if _NOFILM.search(t):
        return 'someone asks not to be filmed'
    return ''



# --------------------------------------------------------------------- gather
def talk_of(an):
    return ' '.join(x[2] for x in an['speech']['lines']) + ' ' + ' '.join(an.get('chat_text') or [])


def relevance(spec, m, an):
    """Second look once we can hear and see the clip."""
    if spec['kind'] != 'topic':
        return 1.0, ''
    speech_txt = ' '.join(x[2] for x in an['speech']['lines'])
    if spec['topic']:
        t = topics.TOPICS[spec['topic']]
        r = topics.match_text(spec['topic'], m['titles'], speech_txt + ' ' + ' '.join(an.get('chat_text') or []))
        if t.get('female'):
            f = (an.get('faces') or {}).get('f', 0)
            if _MINOR.search(speech_txt) or any(_MINOR.search(x) for x in m['titles']):
                return 0.0, 'possible minor mentioned'
            titled = m.get('rel', 0) >= .8
            said = topics.match_text(spec['topic'], [], speech_txt)
            if titled:
                r = .75 + .15 * (said > 0) + .10 * (f >= .12) if f >= .10 or said > 0 else .40
            else:
                # 'girl', 'kiss', 'wife' in a title proves nothing by itself.
                r = .22 * (m.get('rel', 0) >= .5) + min(.6, said) + (.34 if f >= .50 else .24 if f >= .25 else .14 if f >= .10 else -.25)
        elif m.get('rel', 0) >= .8:
            r = max(r, .8)
        return r, ''
    return max(m.get('rel', 0), topics.match_words(spec['words'], m['titles'], speech_txt)), ''


def ensure_chat(sid, path):
    """Find on-screen chat and whether it shows slurs; remembered in the cached analysis."""
    an = analyze.load(core.ANALYSIS, sid)
    if not an:
        return an
    if 'chat_bad' in an:
        bad = speech.chat_has_slur(an.get('chat_text') or [])      # the word list can grow
        if bad != an['chat_bad']:
            an['chat_bad'] = bad
            media.write_json(analyze.cache_path(core.ANALYSIS, sid), an)
        return an
    if not an['layout']['chat'] and an['layout']['mode'] != 'portrait':
        c = ears.find_chat_by_text(path, an['meta'])
        if c:
            an['layout']['chat'] = [c]
            an['chat_text'] = [t for e in (ears.chat_events(path, an['meta'], c) or []) for t in e['text']][:80]
    an['chat_bad'] = speech.chat_has_slur(an.get('chat_text') or [])
    media.write_json(analyze.cache_path(core.ANALYSIS, sid), an)
    return an


def real_life(an):
    """Is the camera on people, rather than a game with a streamer pasted in a corner?"""
    if an['layout']['mode'] != 'irl' or not an['tracks']:
        return False
    dur = max(1.0, an['meta']['duration'])
    main = max(an['tracks'], key=lambda t: t['t'][-1] - t['t'][0])
    cx = np.median(main['cx'])
    pasted = np.std(main['cx']) < .035 and np.std(main['cy']) < .06 and (cx < .34 or cx > .66) and (main['t'][-1] - main['t'][0]) > .45 * dur
    return not pasted


def clear_span(an, payoff, a, b):
    """The part of a..b around the punchline that needs no censoring, when it still tells the joke."""
    bad = an['safety']['censor']
    if any(x - .2 < payoff < y + .2 for x, y in bad):
        return None
    lo = max([a] + [y + .3 for x, y in bad if y <= payoff])
    hi = min([b] + [x - .3 for x, y in bad if x >= payoff])
    if hi - lo < 8 or payoff - lo < 3 or hi - payoff < 1.2:
        return None
    return round(lo, 3), round(hi, 3)


def dark_share(path, a, b, level=16):
    raw = media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-ss', f'{a:.2f}', '-t', f'{max(.5, b - a):.2f}', '-i', path, '-an', '-vf', 'fps=2,scale=64:36,format=gray', '-f', 'rawvideo', 'pipe:1']).stdout
    f = np.frombuffer(raw, np.uint8)
    f = f[:len(f) // 2304 * 2304].reshape(-1, 2304)
    return float((f.mean(axis=1) < level).mean()) if len(f) else 1.0


def ensure_clip(c, m, conf, notify):
    """Download one indexed clip if needed and return its source record (or None)."""
    core.record_sources([index.to_source(c, m)])
    s = core.source(c['id'])
    if s['state'] == 'skipped':
        return None
    if s.get('path') and Path(s['path']).is_file():
        return s
    if (s.get('tries') or 0) >= 4:
        return None
    local = core.SRC / f'{core.safe_slug(s["streamer"])}_{core.safe_slug(s["id"])}.mp4'
    with core.db() as con:
        if con.execute('SELECT 1 FROM source WHERE path=? AND id<>?', (str(local), s['id'])).fetchone():
            local = local.with_name(local.stem[:60] + '_' + hashlib.sha256(s['id'].encode()).hexdigest()[:8] + '.mp4')
    for attempt in range(3):
        try:
            if conf.get('permitted_public_clip_downloads'):
                core.download_permitted_public_clip(s, local)
            else:
                core.Twitch(conf).download_clip(s, local)
            core.update_source(s['id'], state='downloaded' if s['state'] == 'discovered' else s['state'], path=str(local), error=None, sha256=core.file_digest(local))
            return core.source(s['id'])
        except media.Cancelled:
            raise
        except Exception as e:
            if attempt == 2 or '404' in str(e):
                core.update_source(s['id'], error=str(e)[:800], tries=(s.get('tries') or 0) + 1)
                notify(f'FAILED download {s["title"][:40]}: {str(e)[:140]}')
                return None
            time.sleep(2 * 2 ** attempt)


def gather(spec, conf, notify=print, progress=lambda a, b, m='': None, max_downloads=48):
    """Download and judge moments until there is enough good footage. Returns accepted list."""
    if not conf.get('permission_confirmed'):
        raise RuntimeError('Confirm footage permission in Settings first.')
    if core.free_gb() < 6:
        raise RuntimeError(f'Only {core.free_gb():.1f} GB free on the data drive.')
    target = spec['minutes'] * 60
    cands = topics.search(spec, 500)
    if not cands:
        raise RuntimeError('No clips match that idea in the index. Update the index or widen the dates.')
    with core.db() as con:
        # A rejected or failed video gives its moments back.
        used_video = {r['source_id'] for r in con.execute("SELECT u.source_id FROM usage u JOIN production p ON p.id=u.production_id WHERE u.kind='video' AND p.state NOT IN ('rejected','failed')")}
    if spec['kind'] == 'recap' and spec.get('group'):
        # A house recap is about the people, so real-life footage comes first.
        for m in cands:
            home = topics.match_text('house', m['titles']) >= .8
            m['rank'] *= (1.35 if home else .70) if analyze.is_game(m['game']) else (1.2 if home else 1.0) + .10 * min(3, m['people'] - 1)
    cands = [m for m in cands if not (m['flags']['sex'] >= 2 or (m['flags']['sad'] and spec['kind'] != 'recap'))]
    # Clips already watched rank by what is really in them, not by a guess from their titles.
    for m in cands:
        have = core.source(m['best']['id'])
        if have and have.get('path') and analyze.load(core.ANALYSIS, have['id']):
            try:
                an, sc = pr.analyse_source(have, conf)
            except Exception:
                continue
            m['rank'] = 0.0 if an['safety']['level'] == 'hold' else .4 * m['rank'] + .6 * sc['total'] / 100
    # Footage from another long video comes back only when it clearly beats what is new.
    for m in cands:
        if m['key'] in used_video:
            m['rank'] *= .6
    cands.sort(key=lambda m: -m['rank'])
    ok, secs, got, seen_fp, rejected, blind = [], 0.0, 0, [], {}, 0
    need = target * (1.30 if spec['kind'] != 'recap' else 1.2)
    for k, m in enumerate(cands):
        media.check_cancel()
        if secs >= need or got >= max_downloads:
            break
        if spec.get('group') and sum(1 for o in ok if o['m']['streamer'] == m['streamer']) >= max(3, round(.30 * target / 30)):
            rejected['enough of one person'] = rejected.get('enough of one person', 0) + 1
            continue
        if any(abs(m['ts'] - o['m']['ts']) < 50 and m['streamer'] != o['m']['streamer'] for o in ok):
            rejected['same event on another channel'] = rejected.get('same event on another channel', 0) + 1
            continue
        gta = sum(1 for o in ok if o['gameplay'])
        over = spec['topic'] != 'gta' and gta >= (max(3, .18 * (len(ok) + 1)) if spec.get('group') else max(4, (.65 if spec['days'] <= 10 else .30) * (len(ok) + 1)))
        # The category can say GTA while the camera is on people in the house.
        if over and analyze.is_game(m['game']) and not (spec.get('group') and m['people'] >= 2):
            continue
        if spec['kind'] == 'topic' and m.get('rel', 1) < .5:
            blind += 1
            if blind > 14:               # unlabelled guesses rarely pay off; stop listening to everything
                break
        progress(min(.999, secs / need), 1, f'Footage {secs / 60:.1f} of {need / 60:.1f} min. Getting: {(m["titles"] or ["clip"])[0][:44]}')
        had = core.source(m['best']['id'])
        s = ensure_clip(m['best'], m, conf, notify)
        if not s:
            continue
        if not had or not had.get('path'):
            got += 1
        try:
            an, sc = pr.analyse_source(s, conf)
        except media.Cancelled:
            raise
        except Exception as e:
            notify(f'Skipped {s["title"][:36]}: {str(e)[:120]}')
            continue

        def no(why):
            rejected[why] = rejected.get(why, 0) + 1
        if an['safety']['level'] == 'hold' and not s.get('allow_hold'):
            no('held for safety')
            continue
        if an['layout']['mode'] == 'portrait':
            no('portrait clip')
            continue
        why = unfit(an, m['titles'])
        if why:
            no(why)
            continue
        gameplay = analyze.is_game(m['game']) and not real_life(an)
        if over and gameplay:
            no('enough gameplay already')
            continue
        rel, note = relevance(spec, m, an)
        if rel < .5:
            no(note or 'not really on topic')
            continue
        cut = story.cut(an, 58, 'video')
        a, b = cut['keep'][0][0], cut['keep'][-1][1]
        if any(x < b and y > a for x, y in an['safety']['censor']):
            free = clear_span(an, cut['payoff'], a, b)
            if not free:
                no('needs censoring')
                continue
            a, b = free
            cut['keep'] = [list(free)]
        serious = any('not comedy' in w for w in sc['why'])
        if serious and spec['kind'] != 'recap':
            no('serious, not comedy')
            continue
        if sc['total'] < (30 if spec['kind'] == 'topic' else 38 if spec.get('group') or spec['days'] > 10 else 34) and not (s.get('rating') or 0) >= 3:
            no('too weak')
            continue
        if spec['kind'] != 'topic' and sc['total'] < 48 and any('mostly calm talking' in w for w in sc['why']) and not (s.get('rating') or 0) >= 3:
            no('just talking')
            continue
        if dark_share(s['path'], a, b) > .30:
            no('mostly dark picture')
            continue
        fp = core.ensure_fp(s['id'], s['path'])
        dup = False
        for f2 in seen_fp:
            q = audio.match(f2, fp)
            if q and q['ber'] <= audio.SAME_BER and q['seconds'] >= min(6, .35 * len(fp) * audio.FP_HOP):
                dup = True
                break
        if dup:
            no('same moment twice')
            continue
        seen_fp.append(fp)
        it = {'gameplay': gameplay, 'm': m, 's': s, 'an': an, 'sc': sc, 'rel': rel, 'cut': cut, 'value': (.38 * sc['total'] / 100 + .17 * min(1, max(0, m['score'])) + .45 * rel if spec['kind'] == 'topic' else .62 * sc['total'] / 100 + .38 * min(1, max(0, m['score']))) - (.05 if not meta.describe(s) else 0)}
        if m['key'] in used_video:
            it['value'] -= .05
        if spec.get('group') and not gameplay:
            # People together beat one person reacting to a screen or talking to chat.
            if an['layout']['mode'] == 'split':
                it['value'] -= .16
            elif sc['parts']['dialogue'] < 35 and sc['parts']['comedy'] < 42:
                it['value'] -= .08
        ok.append(it)
        secs += min(50, story.length(cut['keep']))
        notify(f'+ {s["streamer"]} "{(meta.describe(s) or s["title"])[:48]}" {story.length(cut["keep"]):.0f}s, score {sc["total"]:.0f}' + (f', {m["people"]} viewers clipped it' if m['people'] > 2 else ''))
    if rejected:
        notify('Left out: ' + ', '.join(f'{v} {k}' for k, v in sorted(rejected.items(), key=lambda q: -q[1])))
    return ok


# ---------------------------------------------------------------------- scenes
def stitch(it, conf, notify=print):
    """Extend a moment with the neighbouring viewer clips of the same stream seconds."""
    parts = [{'id': it['s']['id'], 'path': it['s']['path'], 'a': k[0], 'b': k[1]} for k in it['cut']['keep']]
    chain = index.scene_parts(it['m'])
    if len(chain) < 2 or len(it['cut']['keep']) > 1 or it['an']['safety']['censor']:
        return parts
    recs = []
    for c in chain:
        s = it['s'] if c['id'] == it['s']['id'] else ensure_clip(c, it['m'], conf, notify)
        if not s:
            return parts
        recs.append(s)
    fps = [core.ensure_fp(s['id'], s['path']) for s in recs]
    durs = [media.probe(s['path'])['duration'] for s in recs]
    starts = [0.0]
    for i in range(1, len(recs)):
        q = audio.match(fps[i - 1], fps[i], min_seconds=2.5)
        if not q or q['ber'] > audio.SAME_BER or q['offset'] < 1:
            recs, durs, starts = recs[:i], durs[:i], starts[:i]
            break
        starts.append(starts[-1] + q['offset'])
    if len(recs) < 2 or it['s']['id'] not in [s['id'] for s in recs]:
        return parts
    out = []
    for i, s in enumerate(recs):
        a = 0.0 if i == 0 else max(0.0, starts[i - 1] + durs[i - 1] - .25 - starts[i])
        b = durs[i] - (.25 if i < len(recs) - 1 else 0)
        if s['id'] != it['s']['id'] and s['state'] == 'downloaded':
            core.update_source(s['id'], note='Part of a longer scene')
        if b - a > .6:
            out.append({'id': s['id'], 'path': s['path'], 'a': round(a, 3), 'b': round(b, 3)})
    main = [p for p in out if p['id'] == it['s']['id']]
    if not main:
        return parts
    # Trim dead ends only where the analysed clip is the outer part.
    if out[0]['id'] == it['s']['id']:
        out[0]['a'] = max(out[0]['a'], it['cut']['keep'][0][0])
    if out[-1]['id'] == it['s']['id']:
        out[-1]['b'] = max(out[-1]['a'] + 1, min(out[-1]['b'], it['cut']['keep'][-1][1]))
    if sum(p['b'] - p['a'] for p in out) > 105:
        return parts
    # Every extra part must pass the same safety screen.
    for p in out:
        if p['id'] == it['s']['id']:
            continue
        try:
            an2, _ = pr.analyse_source(core.source(p['id']), conf)
        except Exception:
            return parts
        if an2['safety']['level'] == 'hold' or unfit(an2) or any(x < p['b'] and y > p['a'] for x, y in an2['safety']['censor']):
            return parts
        p['mute'] = an2['safety']['mute']
    return out


def order(items, spec):
    if spec['kind'] == 'recap':
        return sorted(items, key=lambda i: i['m']['ts'])
    rank = sorted(items, key=lambda i: -i['value'])
    if len(rank) < 5:
        return rank
    best, second = rank[0], rank[1]
    rest = rank[2:]
    # Never two of the same kind in a row when it can be helped; energy alternates.
    hi, lo = rest[:len(rest) // 2], rest[len(rest) // 2:][::-1]
    mixed = []
    while hi or lo:
        for q in (hi, lo):
            if not q:
                continue
            last = mixed[-1] if mixed else second
            k = next((i for i, x in enumerate(q) if (x['m']['streamer'], x['m']['game']) != (last['m']['streamer'], last['m']['game'])), 0)
            mixed.append(q.pop(k))
    return [second] + mixed + [best]


def choose(items, spec, notify=print):
    """Trim the accepted pool to the target length, best first, with variety."""
    target = spec['minutes'] * 60
    pool = sorted(items, key=lambda i: -i['value'])
    out, secs, per_hour = [], 0.0, {}
    # A shorter video beats one padded with filler.
    floor = .62 * float(np.mean([i['value'] for i in pool[:5]])) if pool else 0
    for it in pool:
        if secs > 150 and it['value'] < floor:
            break
        hour = (it['m']['streamer'], int(it['m']['ts'] // 3600))
        if per_hour.get(hour, 0) >= (3 if spec['days'] <= 8 else 2):
            continue
        n = sum(p['b'] - p['a'] for p in it.get('parts') or []) or story.length(it['cut']['keep'])
        if secs + n > target * 1.12 and secs > target * .8:
            continue
        per_hour[hour] = per_hour.get(hour, 0) + 1
        out.append(it)
        secs += n
        if secs >= target:
            break
    return out, secs


# ------------------------------------------------------------------- rendering
def lower_third(name, line='', day=''):
    """Quiet corner label; not a subtitle."""
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f1, f2 = gfx.font(38, 'bold'), gfx.font(32, 'bold')
    line = line if len(line) <= 58 else ''
    w = int(max(d.textlength(name, font=f1), gfx.text_width(d, line, f2, 32) if line else 0)) + 60
    h = 108 if line else 68
    x, y = 56, H - 78 - h
    d.rounded_rectangle((x, y, x + w, y + h), 16, fill=(8, 11, 17, 188))
    d.rectangle((x, y + 14, x + 6, y + h - 14), fill=(*gfx.GREEN, 255))
    d.text((x + 28, y + 9), name, font=f1, fill='white')
    if line:
        gfx.put(d, (x + 28, y + 57), line, f2, 32, (206, 214, 224, 255))
    if day:
        f3 = gfx.font(44, 'bold')
        tw = int(d.textlength(day, font=f3))
        d.rounded_rectangle((56, 56, 56 + tw + 52, 130), 16, fill=(8, 11, 17, 188))
        d.text((82, 63), day, font=f3, fill='white')
    return im


def _loud(path, a, b):
    p = subprocess.run([media.tool('ffmpeg'), '-hide_banner', '-nostdin', '-ss', f'{a:.3f}', '-t', f'{b - a:.3f}', '-i', str(path), '-vn', '-af', 'ebur128', '-f', 'null', '-'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=media.NOWIN)
    m = re.findall(r'I:\s+(-?[\d.]+) LUFS', p.stderr.decode('utf-8', 'replace'))
    return float(m[-1]) if m else -24.0


def piece(p, an, card, out, preset='veryfast', crf=20):
    """One cut -> uniform 1080p60 + PCM file. Cached by content."""
    meta_ = media.probe(p['path'])
    L = an['layout'] if an else {'captions': None, 'bars': [0, 0, 0, 0], 'cam': None}
    vf = []
    x0, y0, x1, y1 = L['bars'][2], L['bars'][0], 1 - L['bars'][3], 1 - L['bars'][1]
    if p.get('hide_captions') and L['captions']:
        y1 = min(y1, L['captions']['top'])
    if y1 - y0 < .985 and (p.get('hide_captions') and L['captions']):
        # Keep 16:9: lose a little width too, on the side away from the webcam.
        ch = y1 - y0
        cw = min(x1 - x0, ch)
        cam = L['cam']
        cx = x0 if cam and cam['rect'][0] + cam['rect'][2] / 2 < .5 else (x1 - cw) if cam else x0 + (x1 - x0 - cw) / 2
        vf.append(f'crop=iw*{cw:.4f}:ih*{ch:.4f}:iw*{cx:.4f}:ih*{y0:.4f}')
    pre = ''
    if an and an.get('chat_bad') and L.get('chat'):
        x, y, w_, h_ = L['chat'][0]
        x, y = max(0, x - .012), max(0, y - .012)
        w_, h_ = min(1 - x, w_ + .05), min(1 - y, h_ + .05)
        bx = f'iw*{w_:.4f}:ih*{h_:.4f}:iw*{x:.4f}:ih*{y:.4f}'
        pre = f'split[q0][q1];[q1]crop={bx},boxblur=18:3[q2];[q0][q2]overlay=W*{x:.4f}:H*{y:.4f},'
    vf += [f'scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos', f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2', 'setsar=1', f'fps={FPS}', 'format=yuv420p']
    dur = p['b'] - p['a']
    gain = float(np.clip(-15.0 - _loud(p['path'], p['a'], p['b']), -10, 11)) if meta_['audio'] else 0
    af = [f'volume={gain:.2f}dB', 'acompressor=threshold=-19dB:ratio=2.2:attack=8:release=180', 'alimiter=limit=0.88:level=disabled']
    for a, b in p.get('mute') or []:
        a, b = a - p['a'] - .07, b - p['a'] + .07
        if b > 0 and a < dur:
            af.append(f"volume=0:enable='between(t,{max(0, a):.3f},{b:.3f})'")
    af += [f'afade=t=in:d=0.035', f'afade=t=out:st={max(0, dur - .05):.3f}:d=0.05', 'aresample=48000']
    cmd = [media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-ss', f'{p["a"]:.3f}', '-t', f'{dur:.3f}', '-i', p['path']]
    if card:
        cmd += ['-loop', '1', '-framerate', str(FPS), '-t', '4.6', '-i', card]
        graph = f'[0:v]{pre}{",".join(vf)}[v0];[1:v]format=rgba,fade=t=in:st=0.35:d=0.35:alpha=1,fade=t=out:st=3.9:d=0.5:alpha=1[c];[v0][c]overlay=0:0:eof_action=pass,format=yuv420p[v]'
    else:
        graph = f'[0:v]{pre}{",".join(vf)}[v]'
    if not meta_['audio']:
        cmd += ['-f', 'lavfi', '-t', f'{dur:.3f}', '-i', 'anullsrc=r=48000:cl=stereo']
    amap = '0:a:0' if meta_['audio'] else f'{2 if card else 1}:a:0'
    n = max(1, int(round(dur * FPS)))
    cmd += ['-filter_complex', graph, '-map', '[v]', '-map', amap, '-frames:v', str(n), '-c:v', 'libx264', '-preset', preset, '-crf', str(crf), '-maxrate', '16M', '-bufsize', '28M',
            '-g', '120', '-profile:v', 'high', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2',
            '-af', ','.join(af) + f',apad,atrim=0:{n / FPS:.5f}', '-threads', '6', out]
    media.run(cmd)
    return n / FPS


def build_plan(chosen, spec, conf):
    """Serializable plan: cold open + ordered segments."""
    segs = []
    last_day = ''
    for it in chosen:
        parts = it.get('parts') or [{'id': it['s']['id'], 'path': it['s']['path'], 'a': a, 'b': b} for a, b in it['cut']['keep']]
        for p in parts:
            if p['id'] == it['s']['id']:
                p['mute'] = it['an']['safety']['mute']
        day = datetime.fromtimestamp(it['m']['ts']).strftime('%A, %b %d').replace(' 0', ' ')
        segs.append({'id': it['s']['id'], 'streamer': it['s']['streamer'], 'title': meta.describe(it['s']), 'url': it['s']['url'], 'ts': it['m']['ts'], 'value': round(it['value'], 3), 'score': it['sc']['total'],
                     'people': it['m']['people'], 'why': it['sc']['why'], 'payoff': it['cut']['payoff'], 'parts': parts, 'day': day if spec['kind'] == 'recap' and day != last_day else '', 'on': True,
                     'risk': it['an']['safety']['level'], 'game': it['m']['game']})
        last_day = day
    best = max(segs, key=lambda x: x['value'])
    main = next((p for p in best['parts'] if p['id'] == best['id']), best['parts'][0])
    a = max(main['a'], best['payoff'] - 4.5)
    b = min(main['b'], best['payoff'] + 2.6)
    hook = {'id': best['id'], 'path': main['path'], 'a': round(a, 3), 'b': round(b, 3), 'mute': main.get('mute')} if len(segs) >= 5 and segs[0]['id'] != best['id'] and b - a > 3 else None
    return {'version': 5, 'kind': 'video', 'spec': {k: v for k, v in spec.items()}, 'segments': segs, 'hook': hook, 'voice': [], 'hide_captions': bool(conf.get('video_hide_captions', True)),
            'lower_third': bool(conf.get('video_lower_third', True))}


def render_video(plan, out_path, notify=lambda p, m='': None, log=print):
    segs = [s for s in plan['segments'] if s.get('on', True)]
    if not segs:
        raise media.MediaError('No segments are switched on.')
    CACHE.mkdir(parents=True, exist_ok=True)
    for old in CACHE.glob('*.mkv'):
        if time.time() - old.stat().st_mtime > 14 * 86400:
            old.unlink(missing_ok=True)
    jobs, chapters = [], []
    with tempfile.TemporaryDirectory(prefix='fc_long_') as td:
        td = Path(td)
        if plan.get('hook'):
            jobs.append((plan['hook'] | {'hide_captions': plan['hide_captions']}, None, -1))
        for i, s in enumerate(segs):
            for j, p in enumerate(s['parts']):
                card = None
                if j == 0 and plan['lower_third']:
                    card = td / f'card{i}.png'
                    lower_third(meta.display(s['streamer']), s.get('title') or '', s.get('day') or '').save(card)
                jobs.append((p | {'hide_captions': plan['hide_captions']}, (card, meta.display(s['streamer']), s.get('title') or '', s.get('day') or ''), i))
        files, durs = [None] * len(jobs), [0.0] * len(jobs)
        done = [0]

        def one(k):
            media.check_cancel()
            p, card, i = jobs[k]
            an = ensure_chat(p['id'], p['path'])
            sig = json.dumps([an.get('chat_bad') if an else None, p['path'], Path(p['path']).stat().st_size, p['a'], p['b'], p.get('mute'), p['hide_captions'], card[1:] if card else None, an['layout'] if an else None, 6], default=str)
            f = CACHE / (hashlib.sha256(sig.encode()).hexdigest()[:24] + '.mkv')
            if not f.is_file():
                tmp = f.with_name(f.stem + f'.{k}.part.mkv')
                piece(p, an, card[0] if card else None, tmp)
                os.replace(tmp, f)
            files[k] = f
            durs[k] = media.probe(f)['duration']
            done[0] += 1
            notify(80 * done[0] / len(jobs), f'cutting scene {done[0]} of {len(jobs)}')
        with ThreadPoolExecutor(2) as ex:
            list(ex.map(one, range(len(jobs))))
        # ---- assemble
        notify(82, 'joining')
        lst = td / 'list.txt'
        lst.write_text(''.join(f"file '{f.as_posix()}'\n" for f in files), encoding='utf-8')
        t, last = 0.0, None
        for k, (p, card, i) in enumerate(jobs):
            if i != last and i >= 0:
                chapters.append((0.0 if not chapters else t, i))
            last = i
            t += durs[k]
        joined = td / 'joined.mkv'
        media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', joined])
        final = td / 'final.mp4'
        extra = []
        src_a = joined
        if plan.get('voice'):
            notify(88, 'mixing commentary')
            x = media.pcm(joined, 48000, channels=2)
            for v in plan['voice']:
                render._mix_voice(x, v)
            wav = td / 'mix.wav'
            render._wav(wav, x)
            src_a = wav
        ln = render._loudness(src_a, 'anull')
        af = (f"loudnorm=I=-14:TP=-2:LRA=11:measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}:offset={ln['target_offset']}:linear=true,aresample=48000,alimiter=limit=0.80:attack=2:release=60:level=disabled"
              if ln and ln.get('input_i') not in (None, '-inf') else 'aresample=48000')
        notify(92, 'finishing')
        media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i', joined, *(['-i', src_a] if src_a != joined else []), '-map', '0:v:0', '-map', '1:a:0' if src_a != joined else '0:a:0',
                   '-c:v', 'copy', '-af', af, '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', *extra, final])
        notify(97, 'saving')
        media.place(final, out_path)
    return {'duration': round(t, 2), 'chapters': [(round(a, 1), i) for a, i in chapters]}


# ------------------------------------------------------------------ packaging
def clock(t):
    t = int(t)
    return f'{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}' if t >= 3600 else f'{t // 60}:{t % 60:02d}'


def chapter_lines(plan, chapters, dur):
    segs = [s for s in plan['segments'] if s.get('on', True)]
    rows, last = [], -99
    for t, i in chapters:
        s = segs[i]
        if rows and (t - last < 12 or dur - t < 10):
            continue
        name = s['title'] or f'{meta.display(s["streamer"])}' + (f' in {meta.game_name(s)}' if meta.game_name({'game': s.get('game'), 'title': ''}) else '')
        if s.get('day'):
            name = f'{s["day"].split(",")[0]}: {name}'
        rows.append(f'{clock(t)} {name[:70]}')
        last = t
    return rows if len(rows) >= 3 else []


def title_options(plan, dur):
    sp = plan['spec']
    segs = [s for s in plan['segments'] if s.get('on', True)]
    top = [s for s in sorted(segs, key=lambda s: -s['value']) if s['title']]
    who = 'the Core House' if sp.get('group') == 'core' else ' & '.join(meta.display(w) for w in sp['streamers'][:2])
    poss = who + ("'" if who.endswith('s') else "'s")
    mins = max(1, round(dur / 60))
    lab = sp.get('label') or ''
    out = [sp['title']]
    if sp['kind'] == 'recap':
        out += [f'What happened in {who} this week' if sp['days'] <= 8 else f'What happened in {who}', f'{mins} minutes of chaos in {who}', f'This week in {who} was crazy']
    elif sp['kind'] == 'best':
        out += [f'{poss} funniest moments {lab}'.strip(), f'{mins} minutes of {who} being hilarious', f'{who} {lab or "this month"} was too funny'.strip()]
    else:
        name = topics.TOPICS[sp['topic']]['name'] if sp['topic'] else ' '.join(sp['words'])
        out += [f'{who} {name} for {mins} minutes', f'{poss} best {name} moments' if sp['topic'] != 'rizz' else f'{poss} best rizz moments']
    if top:
        out.append(f'{top[0]["title"][:58]} | {sp["title"]}'[:95])
    res = []
    for o in out:
        o = re.sub(r'\s+', ' ', o).strip()
        o = o[:1].upper() + o[1:]
        if o.lower() not in [x.lower() for x in res]:
            res.append(o[:100])
    return res


def describe_video(plan, chapters, dur, conf):
    sp = plan['spec']
    segs = [s for s in plan['segments'] if s.get('on', True)]
    top = [s['title'] for s in sorted(segs, key=lambda s: -s['value']) if s['title']][:3]
    who = list(dict.fromkeys(s['streamer'] for s in segs))
    lines = [f'{sp["title"]}.' + (f' Including {", ".join(x if x.split()[0].split(chr(39))[0] in meta._NAMES else x[0].lower() + x[1:] for x in top)}.' if top else ''), '']
    ch = chapter_lines(plan, chapters, dur)
    if ch:
        lines += ['Chapters', *ch, '']
    lines += ['Watch them live:'] + [f'{meta.display(w)}: https://www.twitch.tv/{w}' for w in who] + ['']
    camp = conf.get('campaigns') or {}
    lines += [str((camp.get(w) or {}).get('note', '')).strip() for w in who if (camp.get(w) or {}).get('note')]
    if conf.get('attribution_line'):
        lines += [conf['attribution_line'], '']
    tags = ' '.join('#' + re.sub(r'\W', '', meta.display(w)) for w in who[:4]) + (' #CoreBoys' if sp.get('group') == 'core' else '') + ' #Twitch #FunnyMoments'
    lines += ['Original clips:']
    for s in segs:
        row = f'{meta.display(s["streamer"])}: {s["url"]}'
        if len('\n'.join(lines)) + len(row) + len(tags) > 4700:
            break
        lines.append(row)
    return '\n'.join(lines + ['', tags])


def _faces(path, t, an=None, n=10, span=5.0):
    """Sharpest, most expressive, camera-facing face around a moment, without captions or chat."""
    best = None
    L = an['layout'] if an else {'captions': None, 'chat': [], 'cam': None, 'bars': [0, 0, 0, 0]}
    y1 = min(1 - L['bars'][1], L['captions']['top'] if L['captions'] else 1.0)
    for k in range(n):
        try:
            fr = media.grab(path, max(0, t - span * .45 + span * k / (n - 1)))
        except media.MediaError:
            continue
        h, w = fr.shape[:2]
        fr = fr[int(L['bars'][0] * h):int(y1 * h)]
        if L['chat']:
            off = L['bars'][0]
            fr = render.soften(fr.copy(), [[c[0] - .03, (c[1] - off) / (y1 - off) - .02, c[2] + .10, c[3] / (y1 - off) + .05] for c in L['chat']], (0, 0, fr.shape[1], fr.shape[0]))
        h, w = fr.shape[:2]
        sm = cv2.resize(fr, (960, int(960 * h / w)))
        found = vision.detect_faces([sm], thr=.78)[0]
        cam = (L.get('cam') or {}).get('rect')
        if cam:
            top, span_ = L['bars'][0], max(.05, y1 - L['bars'][0])
            found = [f for f in found if cam[0] <= f[0] + f[2] / 2 <= cam[0] + cam[2] and cam[1] <= top + (f[1] + f[3] / 2) * span_ <= cam[1] + cam[3]]
        for f in found:
            if f[2] < .055 or f[0] < .005 or f[0] + f[2] > .995 or f[1] < .0:
                continue
            if any(c[0] - .03 < f[0] + 1.15 * f[2] and c[0] + c[2] + .07 > f[0] - .15 * f[2] and (c[1] - L['bars'][0]) / (y1 - L['bars'][0]) - .02 < f[1] + f[3] and (c[1] + c[3] - L['bars'][0]) / (y1 - L['bars'][0]) + .03 > f[1] - .45 * f[3] for c in L['chat']):
                continue              # blurred chat would sit on the head
            x0, y0, x1, y1_ = int(f[0] * w), int(f[1] * h), int((f[0] + f[2]) * w), int((f[1] + f[3]) * h)
            roi = fr[max(0, y0):y1_, max(0, x0):x1]
            if roi.size < 4000:
                continue
            g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            sharp = float(np.clip(cv2.Laplacian(cv2.resize(g, (128, 128), interpolation=cv2.INTER_AREA), cv2.CV_32F).var() / 420, 0, 1))
            lm = f[5:15]
            expr = float(np.clip((((lm[7] + lm[9]) / 2 - lm[5]) / (abs(lm[5] - (lm[1] + lm[3]) / 2) + 1e-3) - .6) / 1.0, 0, 1))
            front = 1 - min(1, abs((lm[4] - (lm[0] + lm[2]) / 2) / (abs(lm[2] - lm[0]) + 1e-3)) * 1.5)
            v = .26 * min(1, f[2] / .20) + .36 * sharp + .18 * expr + .22 * front + .10 * float(np.clip((g.mean() - 45) / 90, 0, 1))
            if sharp < .12:
                v -= .35
            if best is None or v > best['v']:
                best = {'v': v, 'frame': fr, 'face': f[:4]}
    return best


def thumbnails(plan, folder, n=3):
    """1280x720 options: one big expressive face, a few big words."""
    sp = plan['spec']
    segs = sorted([s for s in plan['segments'] if s.get('on', True)], key=lambda s: -s['value'])
    cands = []
    def look(s):
        p = next((x for x in s['parts'] if x['id'] == s['id']), s['parts'][0])
        return s, _faces(p['path'], s['payoff'], analyze.load(core.ANALYSIS, p['id']))

    def tidy(c):
        """Blur every bit of on-screen text; drop the frame if text touches the head."""
        fr = c['frame'].copy()
        h, w = fr.shape[:2]
        x, y, fw, fh = c['face']
        head = (x - .25 * fw, y - .55 * fh, x + 1.25 * fw, y + 1.15 * fh)
        for x0, y0, x1, y1, t in ears.text_boxes(fr):
            bx = ((x0 - 12) / w, (y0 - 8) / h, (x1 + 12) / w, (y1 + 8) / h)
            small = (y1 - y0) < .06 * h
            if not small:
                continue
            if bx[0] < head[2] and bx[2] > head[0] and bx[1] < head[3] and bx[3] > head[1]:
                return False
            fr = render.soften(fr, [[bx[0], bx[1], bx[2] - bx[0], bx[3] - bx[1]]], (0, 0, w, h))
        c['frame'] = fr
        return True
    with ThreadPoolExecutor(4) as ex:
        looked = list(ex.map(look, segs[:12]))
    for s, f in looked:
        if f:
            f['v'] += .12 * s['value']
            f['seg'] = s
            cands.append(f)
    if not sp.get('group') and sp['kind'] != 'topic':
        for c in cands:
            c['v'] -= .20 * (c['seg']['streamer'] != sp['streamers'][0]) + .30 * bool(_SPORTS.search(c['seg']['title'] or ''))
    cands.sort(key=lambda c: -c['v'])
    cands = [c for c in cands if tidy(c)] or cands
    who = 'CORE HOUSE' if sp.get('group') == 'core' else meta.display(sp['streamers'][0]).upper()
    lines = {'recap': [who, 'THIS WEEK' if sp['days'] <= 8 else 'RECAP'], 'best': ['BEST OF', who]}.get(sp['kind'])
    if not lines:
        key = {'rizz': 'RIZZ', 'rage': 'CRASHES OUT', 'fails': 'FAILS', 'pranks': 'PRANKS', 'fights': 'ROASTED', 'food': 'EATS', 'gym': 'MOGS', 'scary': 'SCARED', 'travel': 'TRAVEL CHAOS', 'celebs': 'MEETS'}.get(sp['topic'] or '', ' '.join(sp['words'])[:14].upper())
        lines = [who, key]
    out = []
    if sp.get('group'):
        trio = {}
        for c in cands:
            cam = (analyze.load(core.ANALYSIS, c['seg']['id']) or {'layout': {}})['layout'].get('cam')
            c['v'] += .10 * bool(cam)          # on a webcam it is certainly the streamer
            if c['seg']['streamer'] not in trio or c['v'] > trio[c['seg']['streamer']]['v']:
                trio[c['seg']['streamer']] = c
        trio = sorted(trio.values(), key=lambda c: -c['v'])[:3]
        if len(trio) == 3:
            f = folder / 'thumbnail_1.jpg'
            _panels(trio, lines, f)
            out.append(str(f))
    for k, c in enumerate(cands[:n - len(out)]):
        f = folder / f'thumbnail_{len(out) + 1}.jpg'
        _thumb(c, lines, f, second=cands[(k + 1) % len(cands)] if len(cands) > 1 and k == 2 else None)
        out.append(str(f))
    if not out:
        fr = media.grab(segs[0]['parts'][0]['path'], segs[0]['payoff'])
        f = folder / 'thumbnail_1.jpg'
        _thumb({'frame': fr, 'face': [.6, .2, .15, .3]}, lines, f)
        out.append(str(f))
    return out


def _panels(cands, lines, target):
    """Three people side by side, words along the bottom: for group videos."""
    im = np.zeros((720, 1280, 3), np.uint8)
    pw = 1280 / 3
    for i, c in enumerate(cands):
        fr, (x, y, w, h) = c['frame'], c['face']
        H_, W_ = fr.shape[:2]
        need = pw + 60                                    # slanted edges need a little extra
        k = max(720 * .42 / (h * H_), 720 / H_, need / W_)
        cw, ch = need / k, 720 / k
        x0 = float(np.clip((x + w / 2) * W_ - cw / 2, 0, W_ - cw))
        y0 = float(np.clip((y + h / 2) * H_ - ch * .40, 0, H_ - ch))
        M = np.array([[k, 0, -x0 * k], [0, k, -y0 * k]], np.float64)
        pn = cv2.warpAffine(fr, M, (int(need), 720), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)
        pn = gfx._pop(pn)
        pn = cv2.addWeighted(pn, 1.45, cv2.GaussianBlur(pn, (0, 0), 2.0), -.45, 0)
        ox = int(i * pw - 30)
        full = np.zeros_like(im)
        a, b_ = max(0, ox), min(1280, ox + pn.shape[1])
        full[:, a:b_] = pn[:, a - ox:b_ - ox]
        m = np.zeros((720, 1280), np.uint8)
        L_ = -200 if i == 0 else int(i * pw)
        R_ = 1480 if i == 2 else int((i + 1) * pw)
        cv2.fillPoly(m, [np.array([[L_ + 26, 0], [R_ + 26, 0], [R_ - 26, 720], [L_ - 26, 720]])], 255)
        im[m > 0] = full[m > 0]
    for i in (1, 2):
        cv2.line(im, (int(i * pw) + 26, 0), (int(i * pw) - 26, 720), (255, 255, 255), 8, cv2.LINE_AA)
    pil = Image.fromarray(im[:, :, ::-1]).convert('RGBA')
    g = np.zeros((720, 1280), np.uint8)
    g[:] = np.clip(np.interp(np.arange(720), [430, 560, 720], [0, 150, 235]), 0, 255).astype(np.uint8)[:, None]
    pil = Image.composite(Image.new('RGBA', pil.size, (5, 8, 14, 255)), pil, Image.fromarray(g))
    d = ImageDraw.Draw(pil)
    size = 150
    gap = .28
    while size > 50 and sum(d.textlength(t, font=gfx.font(size, 'black')) for t in lines) + gap * size * (len(lines) - 1) > 1190:
        size -= 4
    fnt = gfx.font(size, 'black')
    tw = sum(d.textlength(t, font=fnt) for t in lines) + gap * size * (len(lines) - 1)
    xx = 640 - tw / 2
    for i, t in enumerate(lines):
        d.text((xx, 692), t, font=fnt, anchor='ls', fill=(255, 255, 255) if i == 0 else gfx.GOLD, stroke_width=max(6, size // 15), stroke_fill=(0, 0, 0))
        xx += d.textlength(t, font=fnt) + gap * size
    pil.convert('RGB').save(target, 'JPEG', quality=93)


def _thumb(c, lines, target, second=None):
    fr, (x, y, w, h) = c['frame'], c['face']
    H_, W_ = fr.shape[:2]
    cx, cy = (x + w / 2) * W_, (y + h / 2) * H_
    # Face about 46 % of the height, always well right of centre; the rest of the frame
    # is mirrored out under the dark side so text never has to sit on a face.
    ch = float(np.clip(h * H_ / .46, H_ * .28, H_))
    k = 720 / ch
    M = np.array([[k, 0, 1280 * .735 - cx * k], [0, k, 0]], np.float64)
    yy0 = float(np.clip(cy - ch * .47, 0, H_ - ch))
    M[1, 2] = -yy0 * k
    im = cv2.warpAffine(fr, M, (1280, 720), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)
    soft = cv2.GaussianBlur(im, (0, 0), 14)
    edge = max(0.0, M[0, 2])                      # where real picture begins
    edge2 = M[0, 2] + W_ * k                      # where it ends
    xs = np.arange(1280, dtype=np.float32)
    mask = np.clip((xs - edge) / 90, 0, 1) * np.clip((edge2 - xs) / 90, 0, 1)
    im = (im * mask[None, :, None] + soft * (1 - mask[None, :, None])).astype(np.uint8)
    im = gfx._pop(im)
    im = cv2.addWeighted(im, 1.5, cv2.GaussianBlur(im, (0, 0), 2.2), -.5, 0)
    pil = Image.fromarray(im[:, :, ::-1]).convert('RGBA')
    left = True
    g = np.zeros((720, 1280), np.uint8)
    g[:] = np.clip(np.interp(np.arange(1280), [0, 520, 800], [228, 190, 0]), 0, 255).astype(np.uint8)
    pil = Image.composite(Image.new('RGBA', pil.size, (5, 8, 14, 255)), pil, Image.fromarray(g))
    d = ImageDraw.Draw(pil)
    size = 170
    while size > 60 and max(d.textlength(t, font=gfx.font(size, 'black')) for t in lines) > 610:
        size -= 6
    fnt = gfx.font(size, 'black')
    th = int(size * 1.02)
    yy = 360 - th * len(lines) / 2 - size * .16
    for i, t in enumerate(lines):
        tw = d.textlength(t, font=fnt)
        xx = 52 if left else 1280 - 52 - tw
        d.text((xx, yy + i * th), t, font=fnt, fill=(255, 255, 255) if i == 0 else gfx.GOLD, stroke_width=max(6, size // 16), stroke_fill=(0, 0, 0))
    pil.convert('RGB').save(target, 'JPEG', quality=93)


_NOFILM = re.compile(r"(?i)(stop (recording|filming)|(don'?t|do not|not) (record|film) (me|us|my)|turn (the|that|your) camera off|not allowed to (film|record))")
_SPORTS = re.compile(r'(?i)\b(nba|nfl|ufc|half ?court|heat vs|vs heat|lakers|world cup|super bowl|knicks|arena)\b')


def used_reasons(seg, an):
    """Safety notes about seconds that are really in the video."""
    part = [(p['a'], p['b']) for p in seg['parts'] if p['id'] == seg['id']]
    return [r for r in an['safety']['reasons'] if not r['t'] or not part or any(x < b and y > a for x, y in r['t'] for a, b in part)]


def check(path, expected):
    m = media.probe(path)
    issues = []
    if (m['w'], m['h']) != (W, H):
        issues.append(f"Wrong size {m['w']}x{m['h']}")
    if abs(m['duration'] - expected) > max(1.0, expected * .004):
        issues.append(f"Length {m['duration']:.1f}s differs from plan {expected:.1f}s")
    if not m['audio'] or m['sample_rate'] != 48000:
        issues.append('Audio problem')
    from . import qc
    i, pk = qc._loud(path)
    if i is not None and not -16.5 <= i <= -12:
        issues.append(f'Loudness {i:.1f} LUFS')
    raw = media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-skip_frame', 'nokey', '-i', path, '-an', '-vf', 'scale=160:90,format=gray', '-fps_mode', 'passthrough', '-f', 'rawvideo', 'pipe:1']).stdout
    f = np.frombuffer(raw, np.uint8)
    f = f[:len(f) // 14400 * 14400].reshape(-1, 90, 160)
    black = float((f.mean(axis=(1, 2)) < 8).mean()) if len(f) else 1.0
    if black > .06:
        issues.append(f'{black * 100:.0f}% of sampled frames are black')
    return {'ok': not issues, 'issues': issues, 'metrics': {'seconds': round(m['duration'], 1), 'fps': round(m['fps'], 2), 'mb': round(m['bytes'] / 1e6), 'lufs': i, 'peak_db': pk, 'black': round(black, 3), 'sample_rate': m['sample_rate'], 'face_share': None}}


# -------------------------------------------------------------------- top level
def package(pid, plan, res, conf):
    folder = core.REVIEW / pid
    video = folder / 'video.mp4'
    report = check(video, res['duration'])
    shots = thumbnails(plan, folder)
    thumb = folder / 'thumbnail.jpg'
    shutil.copyfile(shots[min(int(plan.get('thumb_pick', 0)), len(shots) - 1)], thumb)
    opts = title_options(plan, res['duration'])
    title = plan.get('title') or opts[0]
    desc = plan.get('description') or describe_video(plan, res['chapters'], res['duration'], conf)
    segs = [s for s in plan['segments'] if s.get('on', True)]
    flags = []
    for s in segs:
        an = analyze.load(core.ANALYSIS, s['id'])
        rep = dict(an['safety'], reasons=used_reasons(s, an)) if an else {}
        for f in safety.flags(rep) if an else []:
            if 'Suggestive visuals' in f and an['layout']['mode'] != 'split':
                continue          # ordinary clothes trip this on real cameras; nudity still reports
            if f.startswith(('HOLD', 'CHECK')):
                flags.append(f'[{meta.display(s["streamer"])}: {(s["title"] or "clip")[:26]}] {f}')
    bad = [s for s in segs if (analyze.load(core.ANALYSIS, s['id']) or {}).get('chat_bad')]
    if bad:
        flags.append(f'Note: stream chat blurred in {len(bad)} scene(s) because it shows a slur')
    for s in segs:
        an = analyze.load(core.ANALYSIS, s['id'])
        if an and re.search(r'(?i)(roulette|blackjack|casino|slot machine|gambl)', talk_of(an) + ' ' + (s['title'] or '')):
            flags.append(f'CHECK: [{meta.display(s["streamer"])}: {(s["title"] or "clip")[:26]}] gambling; make sure no betting site is promoted')
    for s in segs:
        an = analyze.load(core.ANALYSIS, s['id'])
        if an and _NOFILM.search(talk_of(an)):
            flags.append(f'CHECK: [{meta.display(s["streamer"])}: {(s["title"] or "clip")[:26]}] someone seems to ask not to be filmed; they could file a privacy complaint')
    for s in segs:
        src = core.source(s['id']) or {}
        if _SPORTS.search(' '.join(meta.crowd_titles(src)) + ' ' + (s['title'] or '')):
            flags.append(f'CHECK: [{meta.display(s["streamer"])}: {(s["title"] or "clip")[:26]}] sports event; broadcast pictures and arena music can be claimed')
    for s in segs:
        an = analyze.load(core.ANALYSIS, s['id'])
        if an and len(_ETHNIC.findall(talk_of(an))) >= 2:
            flags.append(f'CHECK: [{meta.display(s["streamer"])}: {(s["title"] or "clip")[:26]}] jokes about ethnicity; your call')
    flags += ['QUALITY: ' + i for i in report['issues']]
    flags.append('Advisory only: watch the whole video before approving')
    level = 'caution' if any('CHECK' in f for f in flags) else 'clear'
    who = list(dict.fromkeys(s['streamer'] for s in segs))
    tags = []
    for w in who:
        tags += [meta.display(w), f'{meta.display(w)} clips', f'{meta.display(w)} funny moments']
    tags += ['core boys', 'core house'] if plan['spec'].get('group') == 'core' else []
    tags += ['twitch highlights', 'streamer funny moments']
    media.write_json(folder / 'review.json', {'id': pid, 'kind': 'video', 'title': title, 'description': desc, 'flags': flags, 'plan': plan, 'quality': report, 'chapters': res['chapters'], 'checklist': pr.CHECKLIST})
    media.write_text(folder / 'title.txt', title)
    media.write_text(folder / 'description.txt', desc)
    core.update_prod(pid, video=str(video), thumbnail=str(thumb), title=title, description=desc, tags=json.dumps(tags[:24]), headline=plan['spec']['title'], flags_json=json.dumps(flags), risk=level,
                     score=round(100 * sum(s['value'] for s in segs) / len(segs), 1), plan_json=json.dumps(plan), options_json=json.dumps({'titles': opts, 'thumbs': shots}), qc_json=json.dumps(report),
                     seconds=res['duration'], sha256=core.file_digest(video), state='review', error=None, sources_json=json.dumps([s['id'] for s in segs]))
    with core.db() as con:
        con.execute('DELETE FROM usage WHERE production_id=?', (pid,))
        con.executemany("INSERT OR IGNORE INTO usage(source_id,production_id,kind) VALUES(?,?,'video')", [(p['id'], pid) for s in segs for p in s['parts']])
    core.event('built', pid, title)
    return report


def make(text, conf, notify=print, progress=lambda a, b, m='': None, minutes=None, max_downloads=48):
    """Plain-English idea -> finished 16:9 video in the review queue."""
    spec = topics.parse(text, conf) if isinstance(text, str) else text
    if minutes:
        spec['minutes'] = minutes
    notify(f'VIDEO: {spec["title"]} - {", ".join(spec["streamers"])}, {spec["days"]} days, about {spec["minutes"]} min')
    pool = gather(spec, conf, notify, lambda a, b, m='': progress(.62 * a / b, 1, m), max_downloads)
    if len(pool) < 4 or sum(story.length(i['cut']['keep']) for i in pool) < 75:
        raise RuntimeError(f'Only {len(pool)} usable moments were found for "{spec["title"]}". Try more days, another person, or a broader topic.')
    chosen, secs = choose(pool, spec, notify)
    # Turn the strongest moments into longer continuous scenes where viewers' clips overlap.
    extra = 0
    for k, it in enumerate(sorted(chosen, key=lambda i: -i['value'])):
        media.check_cancel()
        if extra >= 8 or it['m']['n'] < 3:
            continue
        progress(.62 + .08 * k / len(chosen), 1, 'Extending the best scenes')
        before = story.length(it['cut']['keep'])
        it['parts'] = stitch(it, conf, notify)
        gain = sum(p['b'] - p['a'] for p in it['parts']) - before
        extra += max(0, len({p['id'] for p in it['parts']}) - 1)
        if gain > 4:
            notify(f'Extended "{(meta.describe(it["s"]) or it["s"]["title"])[:40]}" by {gain:.0f}s with clips from other viewers')

    def size(i):
        return sum(p['b'] - p['a'] for p in i.get('parts') or []) or story.length(i['cut']['keep'])
    while len(chosen) > 6 and sum(size(i) for i in chosen) > spec['minutes'] * 60 * 1.12:
        chosen.remove(min(chosen, key=lambda i: i['value']))
    chosen = order(chosen, spec)
    plan = build_plan(chosen, spec, conf)
    pid = 'v' + hashlib.sha256((spec['text'] + datetime.now().strftime('%Y%m%d%H%M%S')).encode()).hexdigest()[:17]
    return finish(pid, plan, conf, notify, lambda p, m='': progress(.70 + .30 * p / 100, 1, m))


def finish(pid, plan, conf, notify=print, progress=lambda p, m='': None):
    prior = core.production(pid)
    with core.db() as con:
        con.execute("INSERT INTO production(id,kind,sources_json,state,title) VALUES(?, 'video', '[]', 'building', ?) ON CONFLICT(id) DO UPDATE SET state='building',error=NULL", (pid, plan['spec']['title']))
    folder = core.REVIEW / pid
    folder.mkdir(exist_ok=True)
    try:
        for s in plan['segments']:
            for p in s['parts']:
                cur = core.source(p['id'])
                if cur and cur.get('path'):
                    p['path'] = cur['path']          # follow files that were archived meanwhile
        if plan.get('hook'):
            cur = core.source(plan['hook']['id'])
            if cur and cur.get('path'):
                plan['hook']['path'] = cur['path']
        res = render_video(plan, folder / 'video.mp4', progress, notify)
        rep = package(pid, plan, res, conf)
        notify(f'READY: {core.production(pid)["title"]} ({clock(res["duration"])}, {sum(1 for s in plan["segments"] if s.get("on", True))} moments' + (')' if rep['ok'] else ', CHECK: ' + '; '.join(rep['issues']) + ')'))
        return pid
    except BaseException as e:
        back = prior['state'] if prior and prior['state'] in ('review', 'approved', 'rejected') and (folder / 'video.mp4').is_file() else 'failed'
        core.update_prod(pid, state=back, error='Stopped' if isinstance(e, media.Cancelled) else str(e)[:900], plan_json=json.dumps(plan))
        raise


def fresh_paths(plan):
    """Clips move to the used folder when a Short is made from them; follow them there."""
    for x in [p for s in plan['segments'] for p in s['parts']] + ([plan['hook']] if plan.get('hook') else []):
        if not Path(x['path']).is_file():
            cur = core.source(x['id'])
            if cur and cur.get('path') and Path(cur['path']).is_file():
                x['path'] = cur['path']
    return plan


def rebuild(pid, conf, plan, notify=print, progress=lambda p, m='': None):
    p = core.production(pid)
    if p['state'] == 'uploaded_private':
        raise RuntimeError('Already uploaded. Rebuild is locked.')
    plan = {**plan}
    fresh_paths(plan)
    plan.pop('description', None)        # chapters move, so the description is written again
    plan['title'] = p['title']
    on = [s for s in plan['segments'] if s.get('on', True)]
    if plan.get('hook') and plan['hook']['id'] not in [s['id'] for s in on]:
        plan['hook'] = None
    return finish(pid, plan, conf, notify, progress)
