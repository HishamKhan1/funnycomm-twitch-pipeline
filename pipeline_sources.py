"""Clips from places other than Twitch clips: links, files, watched folders, Kick clips,
YouTube videos and full-length streams.

Short videos are taken whole. Long ones are listened to first (sound only, a small
download), the moments where people laugh, shout and cheer are found, and only those
seconds are fetched in full quality.

Rules kept on purpose: the owner confirms permission for every channel or link, nothing
is taken in bulk, and there are no logins, cookies or tricks to get past a site that says
no. When a site refuses, save the video yourself and drop it in.
"""
from __future__ import annotations
import hashlib, json, re, subprocess, tempfile, time
from pathlib import Path
from urllib.parse import urlsplit
import numpy as np
from pipeline_core import SRC, Twitch, event, find_duplicate, free_gb, file_digest, record_sources, safe_slug, source, source_rows, settings, update_source
from funnycomm import audio, ears, index, media

SHORT = 100                     # seconds; longer videos are searched for moments
MAX_HOURS = 8
LEAD, TAIL = 36, 13             # seconds kept around a moment so the set-up survives
VIDEO = ('.mp4', '.mov', '.mkv', '.webm', '.m4v', '.ts', '.avi')

# Official channels, checked on YouTube and Kick in November 2026. Editable in the app.
PLACES = {
    'core': {'youtube': '@createownruneverything'}, 'marlon': {'youtube': '@mar3lg'}, 'lacy': {'youtube': '@lacyhimself', 'kick': 'lacy'}, 'silky': {'youtube': '@silkylive'},
    'jasontheween': {'youtube': '@jasontheweenie', 'kick': 'jasontheween'}, 'adapt': {'youtube': '@adaptlive'}, 'stableronaldo': {'youtube': '@stableronaldolive'},
}
SITES = {'kick.com': 'Kick', 'youtube.com': 'YouTube', 'youtu.be': 'YouTube', 'tiktok.com': 'TikTok', 'instagram.com': 'Instagram', 'x.com': 'X', 'twitter.com': 'X', 'reddit.com': 'Reddit',
         'v.redd.it': 'Reddit', 'streamable.com': 'Streamable', 'medal.tv': 'Medal', 'drive.google.com': 'Google Drive', 'dropbox.com': 'Dropbox', 'twitch.tv': 'Twitch', 'facebook.com': 'Facebook', 'fb.watch': 'Facebook'}


class SourceError(RuntimeError):
    pass


def site(url):
    h = (urlsplit(url).hostname or '').lower()
    return next((n for d, n in SITES.items() if h == d or h.endswith('.' + d)), h.removeprefix('www.') or 'link')


def channel(conf, who, place):
    return (conf.get('places') or {}).get(who, {}).get(place) or PLACES.get(who, {}).get(place) or ''


def allowed(conf, key):
    return key in (conf.get('places_ok') or [])


def _quiet():
    class Q:
        def debug(self, m):
            pass
        info = warning = error = debug
    return Q()


def _ydl(**kw):
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        raise SourceError('Run Install_Public_Clip_Downloads.bat once to add link support.')
    o = {'quiet': True, 'no_warnings': True, 'logger': _quiet(), 'noprogress': True, 'socket_timeout': 30, 'retries': 3, 'fragment_retries': 5, **kw}
    try:
        o['ffmpeg_location'] = str(Path(media.tool('ffmpeg')).parent)
    except media.MediaError:
        pass
    return YoutubeDL(o)


def _why(e, where):
    t = re.sub(r'\x1b\[[0-9;]*m', '', str(e))
    low = t.lower()
    if 'private' in low or 'login' in low or 'sign in' in low or 'cookies' in low or 'members' in low:
        return f'{where} wants a sign-in for this one. Save it yourself and use "Add files".'
    if '403' in low or 'forbidden' in low or 'impersonat' in low or 'cloudflare' in low or 'bot' in low:
        return f'{where} refused the download. Save it yourself and use "Add files".'
    if 'unsupported url' in low:
        return f'{where} links are not supported. Save the video and use "Add files".'
    if '404' in low or 'not found' in low or 'unavailable' in low or 'removed' in low:
        return f'{where}: the video is not there any more.'
    return f'{where}: {t[:160]}'


# ------------------------------------------------------------------- listings
def list_kick(slug, sort='view', limit=150):
    with _ydl(extract_flat='in_playlist', playlistend=limit) as y:
        try:
            i = y.extract_info(f'https://kick.com/{slug}/clips?range=all&sort={sort}', download=False)
            es = list(i.get('entries') or [])
        except Exception as e:
            raise SourceError(_why(e, 'Kick'))
    return [{'url': e['url'], 'title': e.get('title') or '', 'views': e.get('view_count') or 0, 'seconds': e.get('duration') or 0, 'ts': e.get('timestamp') or 0,
             'game': ', '.join(e.get('categories') or [])[:40]} for e in es if e.get('url')]


def _iso(d):
    m = re.fullmatch(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', d or '')
    return sum(int(v or 0) * k for v, k in zip(m.groups(), (86400, 3600, 60, 1))) if m else 0


def list_youtube(conf, handle, limit=100):
    """A channel's own uploads through the official YouTube API (a few of the 10,000 daily units)."""
    import pipeline_youtube
    yt = pipeline_youtube.service(conf)
    h = handle.strip().rstrip('/').split('/')[-1]
    c = yt.channels().list(part='contentDetails,snippet', **({'id': h} if h.startswith('UC') and len(h) == 24 else {'forHandle': h if h.startswith('@') else '@' + h})).execute().get('items')
    if not c:
        raise SourceError(f'YouTube has no channel called {handle}.')
    up, name, ids, token = c[0]['contentDetails']['relatedPlaylists']['uploads'], c[0]['snippet']['title'], [], None
    while len(ids) < limit:
        r = yt.playlistItems().list(part='contentDetails', playlistId=up, maxResults=50, pageToken=token).execute()
        ids += [i['contentDetails']['videoId'] for i in r.get('items') or []]
        token = r.get('nextPageToken')
        if not token:
            break
    out = []
    for k in range(0, len(ids[:limit]), 50):
        for v in yt.videos().list(part='snippet,contentDetails,statistics', id=','.join(ids[k:k + 50])).execute().get('items') or []:
            sec = _iso(v['contentDetails'].get('duration'))
            if sec:
                out.append({'url': 'https://www.youtube.com/watch?v=' + v['id'], 'title': v['snippet']['title'], 'views': int(v['statistics'].get('viewCount') or 0), 'seconds': sec,
                            'ts': time.mktime(time.strptime(v['snippet']['publishedAt'][:19], '%Y-%m-%dT%H:%M:%S')), 'game': name, 'licensed': bool(v['contentDetails'].get('licensedContent'))})
    return out


def list_streams(conf, login, limit=40):
    """Whole past broadcasts still on Twitch."""
    tw = Twitch(conf)
    b = tw.broadcaster(login)
    out = []
    for v in tw.get('videos', {'user_id': b['id'], 'type': 'archive', 'first': min(100, limit)}).get('data') or []:
        m = re.findall(r'(\d+)([hms])', v.get('duration') or '')
        sec = sum(int(n) * {'h': 3600, 'm': 60, 's': 1}[u] for n, u in m)
        out.append({'url': v['url'], 'title': v['title'], 'views': v['view_count'], 'seconds': sec, 'ts': index.ts_of(v['created_at']), 'game': 'full stream', 'video_id': v['id'], 'muted': v.get('muted_segments') or []})
    return out


# ------------------------------------------------------------ finding moments
def scan(path, dur, want, notify=lambda m: None, progress=lambda f: None, skip=()):
    """The funniest-sounding moments of a long recording: [(peak second, score)], best first."""
    block = 240.0
    loud, lg, sc, cr, mu = [], [], [], [], []
    t = 0.0
    while t < dur - 5:
        media.check_cancel()
        n = min(block, dur - t)
        x = media.pcm(path, 16000, t, n)
        k = int(round(n))
        x = np.pad(x[:k * 16000], (0, max(0, k * 16000 - len(x))))
        rms = np.sqrt((x.reshape(k, 16000) ** 2).mean(axis=1) + 1e-9)
        loud += list(20 * np.log10(rms))
        tg = ears.tag(x, win=2.0, hop=1.0) if rms.max() > .003 else None
        for dst, key in ((lg, 'laugh'), (sc, 'scream'), (cr, 'crowd'), (mu, 'music')):
            v = (list(tg[key]) if tg else [])[:k]
            dst += v + [0.0] * (k - len(v))
        t += block
        progress(min(1.0, t / dur))
    n = len(loud)
    if n < 20:
        return []
    loud, lg, sc, cr, mu = (np.array(v, float)[:n] for v in (loud, lg, sc, cr, mu))
    med = np.array([np.median(loud[max(0, i - 45):i + 46]) for i in range(n)])
    jump = np.clip((loud - med - 2) / 10, 0, 1)
    music = np.convolve(mu, np.ones(21) / 21, 'same')
    s = audio.smooth(1.0 * lg + .50 * sc + .30 * cr + .45 * jump, 5) * np.clip(1.5 - 1.6 * music, 0, 1)
    out = []
    order = np.argsort(-s)
    for i in order:
        if len(out) >= want or s[i] < .16:
            break
        if i < 8 or i > n - 3 or any(abs(i - p) < LEAD + TAIL + 4 for p, _ in out):
            continue
        if any(a - TAIL < i < b + 4 for a, b in skip):
            continue
        out.append((int(i), float(s[i])))
    return out


# ------------------------------------------------------------------ importing
def _register(uid, who, kind, url, title, seconds, path, created='', views=0, extra=None):
    record_sources([{'id': uid, 'streamer': who, 'source_kind': kind, 'url': url, 'title': title, 'views': views, 'seconds': seconds, 'created': created, 'broadcaster_id': '', **(extra or {})}])
    update_source(uid, state='downloaded', path=str(path), sha256=file_digest(path), error=None)
    d = find_duplicate(uid, path)
    if d:
        update_source(uid, state='existing', note='Same moment as ' + (Path(d['where']).name if d['kind'] == 'library' else f'"{(d.get("title") or "another clip")[:40]}"'))
        return False
    return True


def _fit(src, dst, a=None, n=None):
    """One clean MP4 the editor can seek in."""
    cut = ['-ss', f'{a:.3f}', '-t', f'{n:.3f}'] if a is not None else []
    media.run([media.tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', *cut, '-i', src, '-map', '0:v:0', '-map', '0:a:0?', '-vf', "scale='min(2560,iw)':-2", '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '17',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2', '-movflags', '+faststart', dst])


def _known(uid):
    s = source(uid)
    return bool(s and s['state'] in ('downloaded', 'used', 'existing', 'skipped'))


def _moments_of(uid_base, fetch, wav, dur, who, kind, url, created, views, want, notify, progress, skip=(), extra=lambda a, b: {}):
    """Listen, choose, fetch only the chosen seconds, register them."""
    picks = scan(wav, dur, min(want, max(2, int(dur / 75))), notify, lambda f: progress(.6 * f), skip)
    ids = []
    if not picks:
        notify('No laughter or big reactions found in it.')
        return ids
    notify(f'Found {len(picks)} possible moments; fetching them...')
    for j, (p, s) in enumerate(picks):
        media.check_cancel()
        a, b = max(0.0, p - LEAD), min(dur, p + TAIL)
        uid = f'link:{uid_base}-{int(a)}'
        progress(.6 + .4 * j / len(picks))
        if _known(uid):
            if source(uid)['state'] == 'downloaded':
                ids.append(uid)
            continue
        target = SRC / f'{safe_slug(who)}_{safe_slug(uid)}.mp4'
        try:
            with tempfile.TemporaryDirectory(prefix='fc_sec_') as td:
                out = Path(td) / 'out.mp4'
                fetch(a, b, out)
                m = media.verify_playable(out, 8)
                media.place(out, target)
            if _register(uid, who, kind, url + f'#t={int(a)}', '', m['duration'], target, created, views, extra(a, b)):
                ids.append(uid)
        except media.Cancelled:
            raise
        except Exception as e:
            notify(f'FAILED one moment at {int(a) // 60}:{int(a) % 60:02d}: {str(e)[:120]}')
    return ids


def add_file(path, who, want=10, notify=print, progress=lambda f: None):
    """One local video. -> new source ids"""
    from pipeline_core import import_local
    path = Path(path)
    m = media.probe(path)
    if m['duration'] <= SHORT:
        uid = import_local(path, who)
        if 'already' in uid:
            return []
        s = source(uid)
        d = find_duplicate(uid, s['path'])
        if d:
            update_source(uid, state='existing', note='Same moment as another clip')
            return []
        return [uid]
    if not m['audio']:
        raise SourceError(f'{path.name} has no sound, so its moments cannot be found.')
    st = path.stat()
    base = 'f' + hashlib.sha256(f'{path.name}|{st.st_size}|{m["duration"]:.2f}'.encode()).hexdigest()[:16]
    notify(f'Listening through {path.name[:40]} ({m["duration"] / 60:.0f} min)...')
    return _moments_of(base, lambda a, b, out: _fit(path, out, a, b - a), path, m['duration'], who, 'local', 'Local import: ' + path.name, '', 0, want, notify, progress)


def add_link(url, who, conf=None, want=10, notify=print, progress=lambda f: None, meta_hint=None):
    """One video link. -> new source ids. The caller has confirmed permission."""
    conf = conf or settings()
    url = url.strip()
    if urlsplit(url).scheme not in ('http', 'https'):
        raise SourceError('That is not a web link.')
    if free_gb() < 3:
        raise SourceError('Less than 3 GB free on the data drive.')
    where, hint = site(url), meta_hint or {}
    with _ydl(noplaylist=True, playlistend=40, extract_flat='in_playlist') as y:
        try:
            info = y.extract_info(url, download=False)
        except Exception as e:
            raise SourceError(_why(e, where))
    if info.get('_type') == 'playlist' or info.get('entries') is not None:
        es = [e for e in info.get('entries') or [] if e.get('url') or e.get('webpage_url')][:40]
        if not es:
            raise SourceError(f'{where}: nothing to download at that link.')
        notify(f'{len(es)} videos at that link.')
        ids = []
        for k, e in enumerate(es):
            media.check_cancel()
            try:
                ids += add_link(e.get('url') or e['webpage_url'], who, conf, max(3, want // len(es)), notify, lambda f, k=k: progress((k + f) / len(es)), {'title': e.get('title')})
            except SourceError as er:
                notify('SKIPPED ' + str(er)[:140])
        return ids
    dur = float(info.get('duration') or hint.get('seconds') or 0)
    title = info.get('title') or hint.get('title') or ''
    link = info.get('webpage_url') or url
    base = hashlib.sha256(link.split('&')[0].encode()).hexdigest()[:18]
    created = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(info.get('timestamp') or hint.get('ts') or 0)) if (info.get('timestamp') or hint.get('ts')) else ''
    views = int(info.get('view_count') or hint.get('views') or 0)
    fmt = {'format': 'bv*[protocol^=http]+ba[protocol^=http]/b[protocol^=http][acodec!=none]/bv*+ba/b', 'format_sort': ['res:1440', 'fps', 'vbr'], 'merge_output_format': 'mkv'}
    if not dur or dur <= SHORT:
        uid = 'link:' + base
        if _known(uid):
            return [uid] if source(uid)['state'] == 'downloaded' else []
        target = SRC / f'{safe_slug(who)}_{safe_slug(uid)}.mp4'
        with tempfile.TemporaryDirectory(prefix='fc_link_') as td:
            f = _download(link, td, where, **fmt)
            m = media.probe(f)
            if m['duration'] > SHORT * 1.5:
                return add_file(f, who, want, notify, progress)
            out = Path(td) / 'fit.mp4'
            _fit(f, out)
            m = media.verify_playable(out, 2)
            media.place(out, target)
        clean = title if where in ('Kick', 'Streamable', 'Medal', 'Reddit') and 3 < len(title) < 90 else ''
        return [uid] if _register(uid, who, 'link', link, clean, m['duration'], target, created, views, {'game': (hint.get('game') or where)[:40]}) else []
    if dur > MAX_HOURS * 3600:
        raise SourceError(f'That video is {dur / 3600:.0f} hours long; the limit is {MAX_HOURS}.')
    notify(f'Listening through "{title[:50]}" ({dur / 60:.0f} min)...')
    skip = [(m['offset'], m['offset'] + m['duration']) for m in hint.get('muted') or []]
    vid = re.search(r'twitch\.tv/videos/(\d+)', link)
    with tempfile.TemporaryDirectory(prefix='fc_long_') as td:
        wav = _download(link, td, where, name='sound', format='ba/ba*/worst[acodec!=none]/worst')
        progress(.08)

        def fetch(a, b, out):
            from yt_dlp.utils import download_range_func
            with tempfile.TemporaryDirectory(prefix='fc_part_') as t2:
                f = _download(link, t2, where, download_ranges=download_range_func(None, [(a, b)]), force_keyframes_at_cuts=False, **fmt)
                _fit(f, out)
        extra = (lambda a, b: {'video_id': vid.group(1), 'vod_offset': b, 'game': hint.get('game') or where}) if vid else (lambda a, b: {'game': (hint.get('game') or where)[:40]})
        return _moments_of(base, fetch, wav, dur, who, 'link', link, created, views, want, notify, lambda f: progress(.08 + .92 * f), skip, extra)


def _download(link, folder, where, name='media', **kw):
    with _ydl(outtmpl=str(Path(folder) / (name + '.%(ext)s')), noplaylist=True, **kw) as y:
        try:
            y.download([link])
        except Exception as e:
            raise SourceError(_why(e, where))
    fs = sorted((f for f in Path(folder).glob(name + '*') if f.is_file() and not f.name.endswith(('.part', '.ytdl'))), key=lambda f: -f.stat().st_size)
    if not fs:
        raise SourceError(f'{where}: nothing was downloaded.')
    return fs[0]


# -------------------------------------------------------------- watched folders
def watch(conf, notify=print, progress=lambda f: None, seen_key='watched_files', limit=12):
    """New videos in the owner's extra folders (Google Drive, Dropbox, downloads). Files stay where they are."""
    seen = set(index.kv_get(seen_key) or [])
    names = {w.lower() for w in conf.get('streamers') or []} | {'core'}
    n = 0
    for d in conf.get('watch_folders') or []:
        root = Path(d)
        if not root.is_dir():
            continue
        for f in sorted(root.rglob('*'), key=lambda p: -p.stat().st_mtime if p.is_file() else 0):
            if n >= limit:
                break
            if not f.is_file() or f.suffix.lower() not in VIDEO:
                continue
            st = f.stat()
            key = f'{f.name}|{st.st_size}'
            if key in seen or time.time() - st.st_mtime < 60 or st.st_size < 200_000:
                continue
            who = next((w for w in names if w in str(f.relative_to(root)).lower()), 'core')
            try:
                got = add_file(f, who, 10, notify, progress)
                n += 1
                if got:
                    notify(f'{len(got)} clip(s) from {f.name[:40]}')
            except media.Cancelled:
                raise
            except Exception as e:
                notify(f'Could not use {f.name[:40]}: {str(e)[:100]}')
            seen.add(key)
            index.kv_set(seen_key, sorted(seen)[-4000:])
    return n
