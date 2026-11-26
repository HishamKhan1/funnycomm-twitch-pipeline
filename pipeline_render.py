"""Planning and building Shorts with the v0.4 smart-editing engine.

Public names kept from earlier versions: make_shorts, build_one, exe, inspect.
The v2.4.2 editor in engine/ stays installed as the standalone studio.
"""
from __future__ import annotations
import hashlib, json, re, shutil, tempfile, time
from pathlib import Path
import cv2
from pipeline_core import (ROOT, SRC, REVIEW, USED, ANALYSIS, REPORTS, db, source_rows, source, production, production_rows, update_source, update_prod,
                           file_digest, event, free_gb, vod_overlap)
from funnycomm import analyze, ears, framing, gfx, hq, media, meta, qc, render, safety, speech, story, voice

VERDICT = {'clear': 'Looks clear', 'caution': 'Check listed moments', 'hold': 'Held'}


def exe(name):
    return media.tool(name)


def inspect(path):
    return media.probe(path)


def style_from(conf):
    return {**render.DEFAULT_STYLE, 'energy': float(conf.get('edit_energy', 1.0)), 'headline': bool(conf.get('edit_headline', True)),
            'teaser': bool(conf.get('edit_teaser', True)), 'replay': bool(conf.get('edit_replay', True)), 'sfx': bool(conf.get('edit_sfx', True)),
            'sfx_gain': float(conf.get('edit_sfx_gain', .8)), 'censor': bool(conf.get('edit_censor', True)),
            'mute_slurs': bool(conf.get('edit_mute_slurs', True)), 'blur_chat': bool(conf.get('edit_blur_chat', True)),
            'steady': float(conf.get('edit_energy', .45)) < .8, 'out_h': 1920 if str(conf.get('output_height', '2560')) == '1920' else 2560}


# ------------------------------------------------------------------- analysis
def attach_crowd(s):
    """Fill in how many viewers clipped this moment and what they called it."""
    if s.get('source_kind') != 'clip' or s.get('crowd_titles'):
        return s
    try:
        from funnycomm import index
        m = index.moment_of(s['id'])
    except Exception:
        m = None
    if m:
        ct = json.dumps(m['titles'], ensure_ascii=False)
        update_source(s['id'], people=m['people'], crowd_titles=ct)
        return {**s, 'people': m['people'], 'crowd_titles': ct}
    return s


def cam_priors(streamer):
    """Webcam boxes already confirmed for this streamer, most common first."""
    found = {}
    for s in source_rows():
        if s['streamer'] != streamer or s['state'] not in ('downloaded', 'used', 'existing'):
            continue
        an = analyze.load(ANALYSIS, s['id'])
        cam = an and an['layout'].get('cam')
        if cam and cam['kind'] == 'box' and cam['conf'] >= .9 and not cam.get('from_prior'):
            key = tuple(round(v * 100) for v in cam['rect'])
            found.setdefault(key, [0, cam['rect']])[0] += 1
    return [{'rect': r} for n, r in sorted(found.values(), key=lambda q: -q[0])[:4]]


def analyse_source(s, conf, notify=lambda m: None, force=False):
    an = analyze.analyse(s['path'], s['id'], s['title'] or '', s['streamer'], ANALYSIS, cam_priors(s['streamer']), conf.get('whisper_model', 'auto'), force=force, game=s.get('game') or '')
    dirty = safety.refresh_language(an, s['title'])
    if an.get('as_game') is None:
        an['as_game'] = dirty = analyze.is_game(s.get('game') or '')
    if framing.ensure(an, s['path']) or dirty:
        media.write_json(analyze.cache_path(ANALYSIS, s['id']), an)
    peers = [x for x in source_rows() if x['streamer'] == s['streamer'] and x['source_kind'] == 'clip']
    s = attach_crowd(s)
    sc = story.score(an, s, peers, int(s.get('rating') or 0))
    wild = framing.wildness(an)
    if wild > .5:                                 # hard to follow, however funny
        sc['total'] = round(max(0, sc['total'] - wild), 1)
        if wild > 4:
            sc['why'].append('shaky camera')
    update_source(s['id'], score=sc['total'], score_json=json.dumps(sc), risk=an['safety']['level'], risk_json=json.dumps(an['safety']['reasons']),
                  seconds=an['meta']['duration'])
    return an, sc


def analyse_all(conf, notify=print, progress=lambda a, b: None, ids=None):
    rows = [s for s in source_rows('downloaded') if s.get('path') and Path(s['path']).is_file() and (ids is None or s['id'] in ids)]
    done = 0
    for n, s in enumerate(rows):
        media.check_cancel()
        progress(n, len(rows))
        had = analyze.load(ANALYSIS, s['id'])
        try:
            an, sc = analyse_source(s, conf)
            done += 1
            if not had:
                notify(f'ANALYSED {s["streamer"]} "{(s["title"] or "")[:34]}": score {sc["total"]:.0f}, {an["layout"]["mode"]}, {VERDICT[an["safety"]["level"]]}')
        except media.Cancelled:
            raise
        except Exception as e:
            update_source(s['id'], error=f'Analysis: {str(e)[:400]}')
            notify(f'ANALYSIS FAILED {s["id"]}: {str(e)[:200]}')
    progress(len(rows), len(rows))
    return done


# ------------------------------------------------------------------- planning
def item_for(s, an, sc, kind, conf):
    lay = json.loads(s['layout_json']) if s.get('layout_json') else None
    cut = story.cut(an, float(conf.get('single_max_seconds', 36) if kind == 'single' else conf.get('ranked_item_seconds', 15)), kind)
    if s.get('layout_json') and lay and lay.get('payoff') is not None:
        cut['payoff'] = float(lay['payoff'])
    return {'id': s['id'], 'path': s['path'], 'streamer': s['streamer'], 'cut': cut, 'label': meta.label(s, an, sc['why']),
            'censor': an['safety']['censor'] if an['layout']['mode'] == 'split' else [], 'mute': an['safety']['mute'], 'avoid': an['safety']['suggestive'],
            'layout': {k: v for k, v in (lay or {}).items() if k in ('mode', 'cam')} or None, 'score': sc['total'], 'why': sc['why']}


def out_fps(conf, ans):
    want = str(conf.get('output_fps', '60'))
    top = max(a['meta']['fps'] for a in ans)
    if want == '30':
        return 30
    if want == 'source':
        return 60 if top > 45 else 30 if top > 26 else 24
    return 60 if top > 45 else 30


def choose_look(conf):
    """Square and full-screen Shorts take turns, so both can be judged on YouTube."""
    want = str(conf.get('short_look', 'mix'))
    if want in ('square', 'fill'):
        return want
    n = {'square': 0, 'fill': 0}
    for p in production_rows()[:40]:
        if p['kind'] == 'single' and p.get('plan_json'):
            lk = json.loads(p['plan_json']).get('look_used')
            if lk in n:
                n[lk] += 1
    return 'fill' if n['square'] > n['fill'] else 'square'


def looks_report():
    """Square against full-screen: {look: {'made', 'public', 'views'}} where views is the average of the public ones."""
    from funnycomm import index
    seen = index.kv_get('views') or {}
    out = {k: {'made': 0, 'public': 0, 'views': None, '_v': []} for k in ('square', 'fill')}
    for p in production_rows():
        if p['kind'] == 'single' and p.get('plan_json'):
            lk = json.loads(p['plan_json']).get('look_used')
            if lk in out:
                out[lk]['made'] += 1
                if p.get('live') == 'public' and p['id'] in seen:
                    out[lk]['public'] += 1
                    out[lk]['_v'].append(seen[p['id']])
    for v in out.values():
        v['views'] = round(sum(v['_v']) / len(v['_v'])) if v['_v'] else None
        del v['_v']
    return out


def make_plan(sources, kind, conf, scored=None):
    """Sources in playing order. For rankings the last one is #1."""
    ans, items, scs = [], [], []
    for s in sources:
        an, sc = scored[s['id']] if scored and s['id'] in scored else analyse_source(s, conf)
        ans.append(an)
        scs.append(sc)
        items.append(item_for(s, an, sc, kind, conf))
    plan = {'version': 4, 'kind': kind, 'fps': out_fps(conf, ans), 'style': style_from(conf), 'items': items, 'voice': []}
    if kind == 'single' and plan['style'].get('steady'):
        plan['style']['look'] = choose_look(conf)
    if kind == 'ranking':
        for k, it in enumerate(items):
            it['rank'] = len(items) - k
        opts, head = meta.ranking_titles([{'src': s, 'an': a} for s, a in zip(sources, ans)])
        plan['headline'] = head
    else:
        opts = meta.titles(sources[0], ans[0], scs[0]['why'])
        plan['headline'] = meta.headline(sources[0], ans[0], scs[0]['why'])
        plan['caption'] = meta.caption(sources[0], ans[0], scs[0]['why'], opts[0] if opts else '')
    plan['titles'] = opts
    return plan, ans


def pid_for(kind, ids):
    return hashlib.sha256((kind + ':' + ','.join(ids)).encode()).hexdigest()[:18]


CHECKLIST = ['I watched the whole Short with sound', 'I have permission to use this footage', 'No copyrighted music, movie or TV audio that could be claimed',
             'No nudity, slurs, hate, danger or graphic content', 'Title, headline and thumbnail match what really happens',
             'Crop, audio and cuts look good']


_tw = {}


def music_muted(s, conf):
    """Seconds of this moment Twitch silenced on the original broadcast for copyrighted music."""
    if not s.get('video_id') or s.get('vod_offset') is None or not conf.get('twitch_client_id'):
        return 0.0
    try:
        from pipeline_core import Twitch
        tw = _tw.setdefault('t', Twitch(conf))
        end = float(s['vod_offset'])
        return hq.muted(tw, s['video_id'], end - float(s.get('seconds') or 30), end) or 0.0
    except Exception:
        return 0.0


def vod_of(s):
    """Clip row with its broadcast position filled in from the index when missing."""
    if s.get('video_id') and s.get('vod_offset') is not None or s.get('source_kind') != 'clip':
        return s
    with db() as con:
        r = con.execute('SELECT video_id,vod_offset FROM clip_index WHERE id=?', (s['id'],)).fetchone()
    return {**s, 'video_id': r['video_id'], 'vod_offset': r['vod_offset']} if r and r['video_id'] else s


def repeat_of(s):
    """Title of an earlier Short that already shows this moment, or None."""
    import numpy as np
    from pipeline_core import ensure_fp
    from funnycomm import audio
    used = {}
    for p in production_rows():
        if p['kind'] != 'video' and p['state'] in ('review', 'approved', 'uploaded_private'):
            used.update({i: p['title'] for i in json.loads(p['sources_json'] or '[]')})
    used.pop(s['id'], None)
    if not used:
        return None
    me = vod_of(s)
    for i in used:
        o = source(i)
        if o and vod_overlap(me, vod_of(o)) > .4:
            return used[i]
    try:
        fp = ensure_fp(s['id'], s['path'])
    except Exception:
        return None
    if len(fp) < 40:
        return None
    mine = len(fp) * audio.FP_HOP
    with db() as con:
        rows = con.execute('SELECT id,fp FROM source WHERE fp IS NOT NULL AND length(fp)>160').fetchall()
    for r in rows:
        if r['id'] in used:
            m = audio.match(np.frombuffer(r['fp'], np.uint32), fp)
            if m and m['ber'] <= audio.SAME_BER and m['seconds'] >= min(.35 * mine, 9):
                return used[r['id']]
    return None


def build_one(sources, kind, conf, notify=print, plan=None, progress=lambda p, m='': None, scored=None):
    ids = [x['id'] for x in sources]
    pid = pid_for(kind, ids)
    prior = production(pid)
    if plan is None and prior and prior['state'] in ('review', 'approved', 'uploaded_private', 'rejected'):
        return False
    if prior and prior['state'] == 'uploaded_private':
        raise RuntimeError('This Short was already uploaded. It cannot be rebuilt.')
    if free_gb() < 2:
        raise RuntimeError('Less than 2 GB free on the data drive.')
    with db() as con:
        con.execute('''INSERT INTO production (id,kind,sources_json,state) VALUES (?,?,?,'building')
          ON CONFLICT(id) DO UPDATE SET state='building',error=NULL''', (pid, kind, json.dumps(ids)))
    folder = REVIEW / pid
    folder.mkdir(exist_ok=True)
    try:
        if plan is None:
            plan, ans = make_plan(sources, kind, conf, scored)
        else:
            ans = [(scored or {}).get(s['id'], (None,))[0] or analyse_source(s, conf)[0] for s in sources]
            for it, s in zip(plan['items'], sources):
                it['path'] = s['path']
        for s in sources:
            if not s.get('path') or not Path(s['path']).is_file():
                raise FileNotFoundError('Source file missing: ' + str(s.get('path')))
        with tempfile.TemporaryDirectory(prefix='fc_voice_') as td:
            got = []
            for k, (it, s, an) in enumerate(zip(plan['items'], sources, ans)):
                it.pop('hq', None)
                if conf.get('hq_source', True):
                    progress(1, 'getting the original broadcast picture')
                    media.check_cancel()
                    try:
                        it['hq'] = hq.fetch(vod_of(s), an['meta'], td, f'original{k}', notify)
                    except Exception as e:
                        notify(f'NOTE: original broadcast not used: {str(e)[:80]}')
                if it.get('hq'):
                    got.append({'mbps': it['hq']['mbps'], 'was': it['hq']['was'], 'size': it['hq']['size']})
            use = {**plan, 'voice': []}
            for k, v in enumerate(plan.get('voice') or []):
                if v.get('file') and Path(v['file']).is_file():
                    use['voice'].append(v)
                elif v.get('text'):
                    f = Path(td) / f'v{k}.wav'
                    voice.speak(v['text'], f, v.get('voice', ''), v.get('rate', 1))
                    use['voice'].append({**v, 'file': str(f)})
            video = folder / 'short.mp4'
            t0 = time.time()
            res = render.render(use, ans, video, progress)
            took = time.time() - t0
            plan['look_used'] = (res.get('looks') or [None])[0]
            plan['zooms_used'] = res.get('zooms', 0)
            split = all(a['layout']['mode'] == 'split' for a in ans)
            report = qc.check(video, res['duration'], split, framed=use['style'].get('steady', False))
            # ---- thumbnails (three options, first is default)
            text = plan.get('thumb_text') or plan['headline']
            shots = []
            # Covers come from the original wide footage, framed on a sharp, complete face.
            from funnycomm import longform
            want = plan.get('thumb_text') or meta.hook(sources[0], ans[0], re.sub(r'\s*#\w+', '', plan['headline'] or ''))
            pool = []
            for it, s_, an_ in zip(plan['items'], sources, ans):
                a0, z0 = it['cut']['keep'][0][0], it['cut']['keep'][-1][1]
                pay = float(it['cut'].get('payoff') or (a0 + z0) / 2)
                for c_ in ([pay, a0 + .25 * (z0 - a0), z0 - .12 * (z0 - a0)] if len(plan['items']) == 1 else [pay]):
                    if any(x - .5 <= c_ <= y + .5 for x, y in (it.get('censor') or [])):
                        continue
                    big = it.get('hq')
                    f_ = longform._faces(big['path'] if big else s_['path'], c_ + (big['shift'] if big else 0), an_, n=9, span=3.2)
                    if f_ and f_['v'] > .42:
                        pool.append((f_['v'], f_, s_))
            pool.sort(key=lambda q: -q[0])
            for k, (_, f_, s_) in enumerate(pool[:3]):
                f = folder / f'thumbnail_{k + 1}.jpg'
                gfx.cover(f_['frame'], f_['face'], want, s_['streamer'], f, len(sources) if kind == 'ranking' else None)
                shots.append(str(f))
            for k, q in list(enumerate(res['thumbs'][:3]))[len(shots):]:
                f = folder / f'thumbnail_{k + 1}.jpg'
                face = max(q['faces'], key=lambda z: z[2])[:4] if q['faces'] else (.35, .24, .30, .18)
                gfx.cover(q['frame'], face, want, sources[q['i']]['streamer'], f, len(sources) if kind == 'ranking' else None)
                shots.append(str(f))
            if not shots:
                fr = media.grab(video, min(2.8, res['duration'] * .6))
                f = folder / 'thumbnail_1.jpg'
                gfx.thumbnail(fr, text, sources[0]['streamer'], f, None)
                shots.append(str(f))
            for it in plan['items']:
                it.pop('hq', None)                  # temporary files; fetched again for a rebuild
            plan['original'] = got
        pick = min(int(plan.get('thumb_pick', 0)), len(shots) - 1)
        thumb = folder / 'thumbnail.jpg'
        shutil.copyfile(shots[pick], thumb)
        try:
            qc.contact(video, folder / 'storyboard.jpg')
        except media.MediaError:
            pass
        # ---- text
        title = plan.get('title') or plan['titles'][0]
        desc = plan.get('description') or meta.description(sources, kind, conf, ans[0])
        tags = plan.get('tags') or meta.tags(sources)
        flags = []
        for s, a in zip(sources, ans):
            pre = f'[{meta.display(s["streamer"])}: {(s["title"] or "")[:22]}] ' if len(sources) > 1 else ''
            flags += [pre + f for f in safety.flags(a['safety'])]
        flags += ['QUALITY: ' + i for i in report['issues']]
        seen_ = ears.screen_text(video, res['duration'])
        if seen_ and seen_['slur']:
            flags.append('HOLD: a slur can be read on screen')
        elif seen_ and seen_['swear']:
            flags.append('CHECK: swear words can be read on screen (' + ', '.join(seen_['swear'][:3]) + ')')
        if seen_ and seen_['boxes'] > 16:
            flags.append('CHECK: a lot of small text on screen; check it is easy to follow')
        for x in sources:
            cut_ = music_muted(vod_of(x), conf)
            if cut_ > 1.5:
                flags.append(f'CHECK: Twitch muted {cut_:.0f} s of this on the original stream for copyrighted music. A strike risk; skip it unless you know the music is fine.')
        for s, a in zip(sources, ans):
            cam = a['layout']['cam']
            if cam and cam['conf'] < .7:
                flags.append('CHECK: webcam position is an estimate (keyed or borderless streamer)')
        flags.append('Advisory only: you must still review visuals, music and YouTube policy')
        level = max((a['safety']['level'] for a in ans), key=lambda v: ['clear', 'caution', 'hold'].index(v))
        if seen_ and seen_['slur']:
            level = 'hold'
        transcript = '\n'.join(f'[{meta.display(s["streamer"])}] ' + ' '.join(x[2] for x in a['speech']['lines']) for s, a in zip(sources, ans))
        data = {'id': pid, 'kind': kind, 'title': title, 'description': desc, 'tags': tags, 'sources': [x['url'] for x in sources], 'flags': flags, 'risk': level,
                'checklist': CHECKLIST, 'plan': plan, 'timeline': res['timeline'], 'quality': report, 'render_seconds': round(took, 1),
                'why': [it.get('why') for it in plan['items']], 'cut_notes': [it['cut'].get('why') for it in plan['items']], 'transcript': transcript,
                'built': time.strftime('%Y-%m-%d %H:%M')}
        media.write_json(folder / 'review.json', data)
        media.write_text(folder / 'title.txt', title)
        media.write_text(folder / 'description.txt', desc)
        media.write_text(folder / 'transcript.txt', transcript or '[No speech detected]')
        old_review = None
        update_prod(pid, video=str(video), thumbnail=str(thumb), title=title, description=desc, tags=json.dumps(tags), headline=plan['headline'],
                    flags_json=json.dumps(flags), risk=level, score=round(sum(it.get('score') or 0 for it in plan['items']) / len(plan['items']), 1),
                    plan_json=json.dumps(plan), options_json=json.dumps({'titles': plan['titles'], 'thumbs': shots}), qc_json=json.dumps(report),
                    seconds=res['duration'], sha256=file_digest(video), review_json=old_review, state='review', error=None)
        event('built', pid, title)
        return True
    except media.Cancelled:
        update_prod(pid, state=prior['state'] if prior and prior['state'] in ('review', 'approved', 'rejected') else 'failed', error='Stopped')
        raise
    except Exception as e:
        # A failed rebuild keeps the previous good version reviewable.
        back = prior['state'] if prior and prior['state'] in ('review', 'approved', 'rejected') and (folder / 'short.mp4').is_file() else 'failed'
        update_prod(pid, state=back, error=str(e)[:1200])
        notify(f'BUILD FAILED {pid}: {str(e)[:300]}')
        return False


def rebuild(pid, conf, changes=None, notify=print, progress=lambda p, m='': None):
    """Re-render one Short with edited settings (trim, payoff, layout, text, voice...)."""
    p = production(pid)
    if not p or not p.get('plan_json'):
        raise RuntimeError('This Short was made by an older version; rebuild it from its clips instead.')
    if p['state'] == 'uploaded_private':
        raise RuntimeError('Already uploaded. Rebuild is locked.')
    plan = json.loads(p['plan_json'])
    for k, v in (changes or {}).items():
        if k == 'items':
            for it, ch in zip(plan['items'], v):
                it.update(ch)
        else:
            plan[k] = v
    srcs = [source(i) for i in json.loads(p['sources_json'])]
    if any(s is None for s in srcs):
        raise RuntimeError('A source record is missing.')
    for s, it in zip(srcs, plan['items']):
        if it.get('window'):
            an = analyze.load(ANALYSIS, s['id']) or analyse_source(s, conf)[0]
            keep = story.cut(an, 999, p['kind'], window=it['window'])
            it['cut']['keep'] = keep['keep']
            if it.get('payoff') is None:
                it['cut']['payoff'] = keep['payoff']
        if it.get('payoff') is not None:
            it['cut']['payoff'] = float(it['payoff'])
    for key in ('title', 'description'):
        plan.setdefault(key, p[key])
    ok = build_one(srcs, p['kind'], conf, notify, plan=plan, progress=progress)
    return ok


# -------------------------------------------------------------------- batches
def taste():
    """What the owner keeps and skips in Review, by category and by streamer: {key: (kept, seen)}."""
    seen = {}
    for p in production_rows():
        if p['kind'] != 'single' or p['state'] not in ('approved', 'uploaded_private', 'rejected'):
            continue
        if p['state'] == 'rejected' and (p.get('error') or '') != 'Skipped in review':
            continue                                   # automatic holds say nothing about taste
        ids = json.loads(p['sources_json'] or '[]')
        s = source(ids[0]) if ids else None
        if not s:
            continue
        kept = p['state'] != 'rejected'
        for key in ('game:' + (s.get('game') or '').lower(), 'who:' + (s.get('streamer') or '').lower()):
            k_, n_ = seen.get(key, (0, 0))
            seen[key] = (k_ + kept, n_ + 1)
    return seen


def preference(it, liked):
    """Points to add or take off for what this owner keeps, and for clips that rarely make good Shorts."""
    s, an = it['src'], it['an']
    if (s.get('rating') or 0) >= 3 or str(s['id']).startswith(('local:', 'link:')):
        return 0.0, []
    pts, why = 0.0, []
    k, n = liked.get('game:' + (s.get('game') or '').lower(), (0, 0))
    if n >= 4 and k / n < .55:
        pts -= min(6.0, (.55 - k / n) * 14)
        why.append('you often skip this category')
    k, n = liked.get('who:' + (s.get('streamer') or '').lower(), (0, 0))
    if n >= 4:
        pts -= min(4.0, max(0.0, (.5 - k / n) * 10))
        pts += min(2.0, max(0.0, (k / n - .8) * 10))
    if an['meta']['duration'] < 12:
        pts -= 4
        why.append('very short')
    if .3 < an.get('dark', 0) <= .5:
        pts -= (an['dark'] - .3) * 20
        why.append('dark picture')
    if not meta.headline(s, an, it['score']['why']) and not meta.best_quote(an):
        pts -= 3
        why.append('no clear story or line')
    return pts, why


def eligible(conf, notify=print):
    """Downloaded, analysed, not held, above the quality floor, best first."""
    from funnycomm.longform import unfit
    out, held, weak, crude = [], 0, 0, 0
    mine = {x.strip().lower() for x in conf.get('streamers') or []}
    for s in source_rows('downloaded'):
        if not s.get('path') or not Path(s['path']).is_file():
            continue
        # Footage fetched for a long video of somebody else is not automatically a Short.
        if mine and str(s['id']).startswith('clip:') and (s.get('streamer') or '').lower() not in mine and not (s.get('rating') or 0) >= 3:
            continue
        an = analyze.load(ANALYSIS, s['id'])
        if not an:
            continue
        sc = json.loads(s['score_json']) if s.get('score_json') else None
        if not sc:
            continue
        if an['safety']['level'] == 'hold' and not s.get('allow_hold'):
            held += 1
            continue
        try:
            called = json.loads(s.get('crowd_titles') or '[]') + [s.get('title') or '']
        except ValueError:
            called = [s.get('title') or '']
        from funnycomm import index as _ix
        if (unfit(an, called) or _ix.text_flags(called)['sex'] >= 3) and not (s.get('rating') or 0) >= 3:
            crude += 1
            continue
        serious = any('not comedy' in w for w in sc['why'])
        if (serious or sc['total'] < float(conf.get('min_score', 36))) and not (s.get('rating') or 0) >= 3:
            weak += 1
            continue
        out.append({'src': s, 'an': an, 'score': sc})
    # A picture nobody can see is not funny; look once at the likely picks and remember.
    from funnycomm.longform import dark_share
    out.sort(key=lambda r: -r['score']['total'])
    for it in out[:30]:
        an = it['an']
        if 'dark' not in an:
            try:
                an['dark'] = round(dark_share(it['src']['path'], 0, an['meta']['duration'], 34), 3)
            except media.MediaError:
                an['dark'] = 0.0
            media.write_json(analyze.cache_path(ANALYSIS, it['src']['id']), an)
    murky = [it for it in out[:30] if it['an']['dark'] > .5 and not (it['src'].get('rating') or 0) >= 3]
    if murky:
        notify(f'{len(murky)} clip(s) are too dark to watch on a phone and were left out.')
        out = [it for it in out if it not in murky]
    frozen = [it for it in out[:60] if framing.still(it['an']) and not (it['src'].get('rating') or 0) >= 3]
    if frozen:
        notify(f'{len(frozen)} clip(s) show a still picture or a paused page and were left out.')
        out = [it for it in out if it not in frozen]
    dizzy = [it for it in out[:60] if framing.unsteady(it['an']) > .18 and not (it['src'].get('rating') or 0) >= 3]
    if dizzy:
        notify(f'{len(dizzy)} clip(s) swing around too much to follow and were left out.')
        out = [it for it in out if it not in dizzy]
    if held:
        notify(f'{held} clip(s) held for safety. Review them in the Clips tab.')
    if crude:
        notify(f'{crude} clip(s) with crude sexual talk or private drama were left out.')
    if weak:
        notify(f'{weak} clip(s) below the quality floor (or serious, not comedy) were left out. Rate one 3+ stars to use it anyway.')
    liked = taste()
    for it in out:
        pts, why = preference(it, liked)
        if pts:
            it['score'] = {**it['score'], 'total': round(it['score']['total'] + pts, 1), 'why': it['score']['why'] + why}
    return sorted(out, key=lambda r: -r['score']['total'])


def plan_batch(conf, notify=print):
    """Decide what to make. Returns [(kind, [item,...]), ...] without rendering."""
    pool = eligible(conf, notify)
    max_n = max(1, int(conf.get('max_shorts_per_run', 6)))
    size = max(2, min(5, int(conf.get('ranked_group_size', 3))))
    jobs, used = [], set()
    if conf.get('produce_ranked') and len(pool) >= size + 1 and max_n >= 2:
        # Keep the strongest clip of each streamer for a standalone Short.
        solo = {}
        for it in pool:
            solo.setdefault(it['src']['streamer'], it['src']['id'])
        rest = [it for it in pool if it['src']['id'] not in solo.values()]
        short = [it for it in rest if it['an']['meta']['duration'] <= 40] or rest
        for g in story.pick_rankings(short, size, 1) or story.pick_rankings(pool, size, 1):
            jobs.append(('ranking', g['items']))
            used |= {i['src']['id'] for i in g['items']}
    per, singles = {}, []
    for it in pool:
        if it['src']['id'] not in used and not any(vod_overlap(it['src'], o['src']) > .4 for o in singles):
            singles.append(it)
    # Alternate streamers so a batch does not look like one person's dump.
    lanes = {}
    for it in singles:
        lanes.setdefault(it['src']['streamer'], []).append(it)
    order = []
    while any(lanes.values()):
        for k in sorted(lanes, key=lambda q: -(lanes[q][0]['score']['total'] if lanes[q] else -1)):
            if lanes[k]:
                order.append(lanes[k].pop(0))
    jobs += [('single', [it]) for it in order[:max(0, max_n - len(jobs))]]
    return jobs


def archive_used(srcs):
    """Move originals out of the inbox after a successful export. Copy-verify-then-remove."""
    for s in srcs:
        cur = source(s['id'])
        old = Path(cur['path'])
        if old.parent.resolve() == USED.resolve() or not old.exists():
            update_source(s['id'], state='used')
            continue
        new = USED / old.name
        if new.exists():
            if file_digest(new) == file_digest(old):
                update_source(s['id'], path=str(new), state='used')
                old.unlink(missing_ok=True)
                continue
            new = USED / (old.stem + '_' + str(int(time.time())) + old.suffix)
        shutil.move(str(old), str(new))
        update_source(s['id'], path=str(new), state='used')


def make_shorts(conf, notify=print, progress=lambda a, b, m='': None):
    if not any(s.get('path') and Path(s['path']).is_file() for s in source_rows('downloaded')):
        notify('No fresh downloaded clips. Discover or import authorized footage first.')
        return 0
    if not speech.installed():
        notify('NOTE: speech recognition is not installed. Run Install_Smart_Editing.bat for much better cuts and safety checks.')
    analyse_all(conf, notify, lambda a, b: progress(a, b, 'Analysing clips'))
    jobs = plan_batch(conf, notify)
    if not jobs:
        notify('Nothing strong enough to build right now. Download more clips or rate some in the Clips tab.')
        return 0
    success = 0
    for i, (kind, group) in enumerate(jobs):
        media.check_cancel()
        srcs = [g['src'] for g in group]
        scored = {g['src']['id']: (g['an'], g['score']) for g in group}
        notify(f'BUILD {i + 1}/{len(jobs)}: {kind} - ' + ', '.join(f'{s["streamer"]} "{(s["title"] or "")[:22]}"' for s in srcs))
        if build_one(srcs, kind, conf, notify, progress=lambda p, m='', i=i: progress(i + p / 100, len(jobs), f'Short {i + 1} of {len(jobs)}: {m}'), scored=scored):
            success += 1
            archive_used(srcs)
            p = production(pid_for(kind, [s['id'] for s in srcs]))
            q = json.loads(p['qc_json'])
            notify(f'READY: {p["title"]} ({p["seconds"]:.0f}s' + (', quality OK)' if q['ok'] else ', CHECK: ' + '; '.join(q['issues']) + ')'))
    progress(len(jobs), len(jobs), 'Done')
    return success


# --------------------------------------------------------- approvals & package
def approve(pid, ticks, note=''):
    p = production(pid)
    if not p or p['state'] not in ('review', 'approved'):
        raise RuntimeError('Only Shorts waiting for review can be approved.')
    if not all(ticks.get(c) for c in CHECKLIST):
        raise RuntimeError('Every checklist item must be ticked after you watch the Short.')
    if not Path(p['video'] or '').is_file():
        raise RuntimeError('The video file is missing.')
    update_prod(pid, state='approved', review_json=json.dumps({'ticks': ticks, 'note': note, 'at': time.strftime('%Y-%m-%d %H:%M')}), sha256=file_digest(p['video']))
    event('approved', pid, note)


def reject(pid, reason='', release=False):
    """Reject a Short. release=True puts its clips back so they can be used again."""
    p = production(pid)
    if not p or p['state'] == 'uploaded_private':
        raise RuntimeError('This cannot be rejected.')
    update_prod(pid, state='rejected', error=reason or None)
    event('rejected', pid, reason)
    if release:
        for sid in json.loads(p['sources_json']):
            s = source(sid)
            if s and s['state'] == 'used' and Path(s['path'] or '').is_file():
                update_source(sid, state='downloaded', note='Released from a rejected Short')


def choose_thumb(pid, k):
    p = production(pid)
    opts = json.loads(p.get('options_json') or '{}')
    f = opts['thumbs'][k]
    shutil.copyfile(f, p['thumbnail'])
    plan = json.loads(p['plan_json'])
    plan['thumb_pick'] = k
    update_prod(pid, plan_json=json.dumps(plan))


def set_text(pid, title, description, tags=None):
    title, description = title.strip()[:100], description.strip()[:4900]
    if not title:
        raise RuntimeError('Add a title.')
    p = production(pid)
    ch = {'title': title, 'description': description}
    if tags is not None:
        ch['tags'] = json.dumps(tags)
    if p.get('plan_json'):
        plan = json.loads(p['plan_json'])
        plan.update(title=title, description=description)
        if tags is not None:
            plan['tags'] = tags
        ch['plan_json'] = json.dumps(plan)
    update_prod(pid, **ch)
    media.write_text(REVIEW / pid / 'title.txt', title)
    media.write_text(REVIEW / pid / 'description.txt', description)


def quality_report(path=None):
    """Plain-text summary of everything in the review queue."""
    lines = ['FunnyComm quality report  ' + time.strftime('%Y-%m-%d %H:%M'), '']
    for p in production_rows():
        q = json.loads(p['qc_json']) if p.get('qc_json') else {}
        m = q.get('metrics', {})
        lines.append(f"[{p['state']}] {p['title'] or p['id']}")
        lines.append(f"   {p['kind']}, {p.get('seconds') or 0:.1f}s, score {p.get('score') or 0:.0f}, risk {p.get('risk')}, "
                     f"{m.get('fps', '?')} fps, {m.get('lufs', '?')} LUFS, face visible {int(100 * (m.get('face_share') or 0))}%")
        for i in q.get('issues', []):
            lines.append('   QUALITY: ' + i)
        for f in json.loads(p.get('flags_json') or '[]'):
            if not f.startswith('Advisory'):
                lines.append('   ' + f)
        lines.append('')
    text = '\n'.join(lines)
    path = Path(path or REPORTS / f'quality_{time.strftime("%Y%m%d_%H%M%S")}.txt')
    media.write_text(path, text)
    return path


# -------------------------------------------------------------- one-button batch
DROP = ROOT.parent / 'Drop Clips Here'


def take_drops(conf, notify=print, progress=lambda f: None):
    """Videos the owner saved from campaigns, drives or other sites: Drop Clips Here/<streamer>/file.mp4,
    plus any watched folders. Long videos are searched for their best moments."""
    import pipeline_sources as ps
    from funnycomm import index
    n = 0
    try:
        DROP.mkdir(exist_ok=True)
        for who in list(conf.get('streamers') or []) + ['core']:
            (DROP / who).mkdir(exist_ok=True)
        seen = set(index.kv_get('drop_seen') or [])
        for f in sorted(DROP.glob('*/*')):
            if f.suffix.lower() not in ps.VIDEO or time.time() - f.stat().st_mtime < 20:
                continue
            key = f'{f.parent.name}/{f.name}|{f.stat().st_size}'
            if key in seen:
                continue
            try:
                n += len(ps.add_file(f, f.parent.name.lower(), 10, notify, progress))
            except media.Cancelled:
                raise
            except Exception as e:
                notify(f'Could not import {f.name[:40]}: {str(e)[:100]}')
            seen.add(key)
            index.kv_set('drop_seen', sorted(seen)[-4000:])
        n += ps.watch(conf, notify, progress)
    except OSError as e:
        notify('Drop folder: ' + str(e)[:100])
    if n:
        notify(f'{n} clip(s) added from your folders')
    return n


def make_from(ids, conf, want, notify=print, progress=lambda a, b, m='': None):
    """Judge these clips and turn the best of them into Shorts. Returns how many were made."""
    if not ids:
        return 0
    analyse_all(conf, lambda m: None, lambda a, b_: progress(.5 + .2 * a / max(1, b_), 1, f'Watching and judging: {min(a + 1, b_)} of {b_}'), ids=set(ids))
    ok = [it for it in eligible({**conf, 'min_score': min(30, float(conf.get('min_score', 36))), 'streamers': []}, lambda m: None) if it['src']['id'] in set(ids)]
    if not ok:
        notify('None of these were funny or safe enough to use.')
    made = 0
    for it in ok:
        if made >= want:
            break
        media.check_cancel()
        s = it['src']
        r = repeat_of(s)
        if r:
            update_source(s['id'], state='existing', note=f'Same moment as the Short "{r[:50]}"')
            notify(f'SKIPPED same moment as "{r[:44]}"')
            continue
        if build_one([s], 'single', conf, notify, progress=lambda p_, m='': progress(.7 + .3 * (made + p_ / 100) / max(1, min(want, len(ok))), 1, f'Editing Short {made + 1}: {m}'), scored={s['id']: (it['an'], it['score'])}):
            made += 1
            archive_used([s])
            p = production(pid_for('single', [s['id']]))
            notify(f'READY: {p["title"]} ({p["seconds"]:.0f}s)')
    return made


def _fits(mode, s):
    """Does this clip belong to what the owner asked for: Newest or All-time?"""
    if mode == 'mix':
        return True
    try:
        days = (time.time() - time.mktime(time.strptime((s.get('created') or '')[:10], '%Y-%m-%d'))) / 86400
    except ValueError:
        return True                     # dropped-in files have no date
    return days <= 14 if mode == 'new' else days > 150


def make_batch(conf, want, streamers=None, mode='mix', notify=print, progress=lambda a, b, m='': None, only=None):
    """Find, download, judge and build until `want` new Shorts are ready for review."""
    from pipeline_core import Twitch, record_sources, download_new
    from funnycomm import index
    if not conf.get('permission_confirmed'):
        raise RuntimeError('Open Settings and confirm you have permission to use this footage first.')
    who = [w for w in (streamers or conf['streamers'])]
    if not who:
        raise RuntimeError('Choose at least one streamer.')
    conf = {**conf, 'streamers': who, 'max_downloads_per_run': 400}
    if not only:
        take_drops(conf, notify, lambda f: progress(.01 + .01 * f, 1, 'Looking through your folders'))
    if not only:
        tw = Twitch(conf)
        progress(0, 1, 'Checking Twitch for new clips')
        index.update(tw, who, int(conf.get('index_days', 180)), notify, lambda a, b_, m='': progress(.02, 1, m))
        if mode != 'new':
            index.backfill(tw, who, notify, lambda a, b_, m='': progress(.04, 1, m))
    made, tried, rounds, broken = 0, set(), 0, set()
    while made < want and rounds < 5:
        rounds += 1
        media.check_cancel()
        if only:
            ms = [m for m in only if m['best']['id'] not in tried]
        else:
            jobs = [j for j in plan_batch({**conf, 'max_shorts_per_run': 400}, lambda m: None) if all(_fits(mode, g['src']) for g in j[1]) and not any(g['src']['id'] in broken for g in j[1]) and not production(pid_for(j[0], [g['src']['id'] for g in j[1]]))][:want - made]
            short = want - made - len(jobs)
            ms = index.pick_mix(who, max(6, int(short * 1.7) + 2), mode, notify if rounds == 1 else lambda m: None) if short > 0 or rounds == 1 and mode != 'mix' else []
        fresh = []
        if ms:
            record_sources([index.to_source(m['best'], m) for m in ms])
            fresh = [m['best']['id'] for m in ms]
            tried |= set(fresh)
            notify(f'Downloading {len(fresh)} clips...')
            download_new(conf, lambda m: notify(m) if m.startswith(('FAILED', 'HELD', 'SKIPPED')) else None, lambda a, b_: progress(.06 + .10 * a / max(1, b_), 1, f'Downloading clip {min(a + 1, b_)} of {b_}'), only=set(fresh))
        # Judge the new downloads, plus anything the owner dropped in; never the whole library.
        look = set(fresh) | {x['id'] for x in source_rows('downloaded') if str(x['id']).startswith(('local:', 'link:'))}
        if not only and not fresh and short > 0:
            spare = [x['id'] for x in source_rows('downloaded') if x['streamer'] in who and x.get('note') != 'Part of a longer scene' and not analyze.load(ANALYSIS, x['id'])]
            look |= set(spare[:12])
        if look:
            analyse_all(conf, lambda m: None, lambda a, b_: progress(.16 + .34 * a / max(1, b_), 1, f'Watching and judging clips: {min(a + 1, b_)} of {b_}'), ids=look)
        if only:
            ok = {it['src']['id']: it for it in eligible({**conf, 'min_score': 0, 'streamers': []}, notify)}
            jobs = [('single', [ok[i]]) for i in fresh if i in ok]
            for i in fresh:
                if i not in ok:
                    s = source(i)
                    notify(f'SKIPPED "{(s or {}).get("title", i)[:40]}": ' + (s.get('note') or 'held for safety, crude talk, too dark or already used' if s else 'not available'))
        else:
            jobs = [j for j in plan_batch({**conf, 'max_shorts_per_run': 400}, notify if rounds == 1 else lambda m: None) if all(_fits(mode, g['src']) for g in j[1]) and not any(g['src']['id'] in broken for g in j[1]) and not production(pid_for(j[0], [g['src']['id'] for g in j[1]]))][:want - made]
        if not jobs:
            if only or not ms:
                break
            continue
        for i, (kind, group) in enumerate(jobs):
            media.check_cancel()
            srcs = [g['src'] for g in group]
            hit = [x for x in srcs if music_muted(vod_of(x), conf) > 1.5] if not only else []
            if hit:
                for x in hit:
                    update_source(x['id'], state='skipped', note='Twitch muted this on the original stream for copyrighted music')
                notify(f'SKIPPED "{(hit[0]["title"] or "")[:40]}": Twitch muted it on the original stream for copyrighted music')
                broken |= {x['id'] for x in srcs}
                continue
            again = [(x, r) for x, r in ((x, repeat_of(x)) for x in srcs) if r]
            if again:
                for x, r in again:
                    update_source(x['id'], state='existing', note=f'Same moment as the Short "{r[:50]}"')
                notify(f'SKIPPED same moment as "{again[0][1][:44]}"')
                broken |= {x['id'] for x in srcs}
                continue
            base = .5 + .5 * (made / max(1, want))
            scored = {g['src']['id']: (g['an'], g['score']) for g in group}
            if build_one(srcs, kind, conf, notify, progress=lambda p_, m='': progress(base + .5 / max(1, want) * p_ / 100, 1, f'Editing Short {made + 1} of {want}: {m}'), scored=scored):
                made += 1
                archive_used(srcs)
                p = production(pid_for(kind, [s['id'] for s in srcs]))
                notify(f'READY: {p["title"]} ({p["seconds"]:.0f}s)')
            else:
                broken |= {s['id'] for s in srcs}          # try something else in this batch
            if made >= want:
                break
        if only:
            break
    progress(1, 1, 'Done')
    return made


def quick_approve(pid, title, conf):
    """Approve after the owner watched it; the checklist is shown on screen next to the button."""
    from pipeline_core import assign_slot
    p = production(pid)
    set_text(pid, title or p['title'], p['description'])
    approve(pid, {c: True for c in CHECKLIST}, 'Watched and approved in quick review')
    return assign_slot(conf, pid)
