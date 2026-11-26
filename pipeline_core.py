"""FunnyComm pipeline core: settings, database, Twitch discovery/downloads, duplicate
library and the review-gated private YouTube upload.

No downloaded clip is assumed to be licensed merely because it is public, and
nothing in this program can publish a video publicly.
"""
from __future__ import annotations
import hashlib, json, os, random, re, shutil, sqlite3, subprocess, tempfile, threading, time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import requests

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('FUNNYCOMM_DATA') or ROOT / 'Pipeline_Data')
SRC = DATA / '01_Downloaded_Sources'
REVIEW = DATA / '02_Review_Queue'
USED = DATA / '03_Used_Sources'
REPORTS = DATA / '04_Reports'
ANALYSIS = DATA / '05_Analysis'
BACKUPS = DATA / '06_Backups'
for folder in (SRC, REVIEW, USED, REPORTS, ANALYSIS, BACKUPS):
    folder.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / 'pipeline.db'
CONF_PATH = DATA / 'settings.json'
VERSION = '0.8.0'
DB_VERSION = 6

DEFAULTS = {
    'streamers': [], 'twitch_client_id': '', 'twitch_client_secret': '',
    'twitch_user_token': '', 'twitch_refresh_token': '', 'twitch_editor_id': '',
    'youtube_client_secret_path': '', 'permission_confirmed': False,
    'allow_owner_highlight_download': False, 'discover_days': 60,
    'max_clips_per_streamer': 50, 'max_highlights_per_streamer': 4,
    'max_highlight_seconds': 480, 'max_downloads_per_run': 12,
    'max_shorts_per_run': 6, 'ranked_group_size': 3, 'produce_ranked': True,
    'voiceover_text': '', 'ffmpeg_fps': 30, 'whisper_model': 'auto',
    'safe_upload_requires_local_approval': True, 'youtube_category_id': '20',
    'export_uncertain_full': False, 'twitch_allow_yt_dlp_for_authorized': False,
    'permitted_public_clip_downloads': False,
    # --- v0.4 editing
    'output_fps': '60', 'single_max_seconds': 36, 'ranked_item_seconds': 15, 'min_score': 36,
    'edit_energy': 0.45, 'edit_headline': True, 'edit_teaser': False, 'edit_replay': False, 'edit_sfx': True, 'edit_sfx_gain': 0.35,
    'edit_censor': True, 'edit_mute_slurs': True, 'edit_blur_chat': True,
    # --- v0.4 metadata / campaigns
    'campaigns': {}, 'attribution_line': 'Clips used with permission.', 'paid_promotion': '',
    # --- v0.5 long videos and calmer style
    'groups': {}, 'video_minutes': 10, 'video_hide_captions': True, 'video_lower_third': True, 'index_days': 180, 'allow_shorts_clips_in_videos': True,
    # --- v0.6 posting and storage
    'posting_mode': 'private', 'posts_per_day': 20, 'post_from': '08:00', 'post_until': '23:00', 'auto_post': True, 'clip_mix': 'mix', 'show_videos': False, 'auto_free': False,
    # --- v0.7 picture quality and more places to get clips
    'hq_source': True, 'output_height': '2560', 'places': {}, 'places_ok': [], 'watch_folders': [],
    # --- v0.8 square and full-screen Shorts take turns ('mix'), or always 'square' / 'fill'
    'short_look': 'mix',
    'settings_version': 0,
}


# ------------------------------------------------------------------ settings
def settings():
    try:
        val = json.loads(CONF_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        val = {}
    return {**DEFAULTS, **val}


def save_settings(opts):
    """Atomic, local-only. Credentials never leave this file."""
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CONF_PATH.with_name('settings.json.tmp')
    tmp.write_text(json.dumps({**DEFAULTS, **opts}, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, CONF_PATH)
    try:
        if os.name == 'nt':
            subprocess.run(['icacls', str(CONF_PATH), '/inheritance:r', '/grant:r', f'{os.environ.get("USERNAME", "Users")}:(R,W,D)'],
                           capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            os.chmod(CONF_PATH, 0o600)
    except Exception:
        pass


# ------------------------------------------------------------------ database
SCHEMA = '''
CREATE TABLE IF NOT EXISTS source (
 id TEXT PRIMARY KEY, streamer TEXT NOT NULL, source_kind TEXT NOT NULL,
 url TEXT NOT NULL, title TEXT, views INTEGER DEFAULT 0, seconds REAL DEFAULT 0,
 created TEXT, broadcaster_id TEXT, state TEXT DEFAULT 'discovered',
 path TEXT, error TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS production (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, sources_json TEXT NOT NULL,
 video TEXT, thumbnail TEXT, title TEXT, description TEXT,
 state TEXT NOT NULL DEFAULT 'building', flags_json TEXT DEFAULT '[]',
 youtube_id TEXT, thumbnail_set INTEGER DEFAULT 0,
 added TEXT DEFAULT CURRENT_TIMESTAMP, error TEXT
);
CREATE TABLE IF NOT EXISTS clip_skip (
 id TEXT PRIMARY KEY, path TEXT, reason TEXT NOT NULL, added TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS existing_video (
 path TEXT PRIMARY KEY, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
 sha256 TEXT, clip_id TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS clip_index (
 id TEXT PRIMARY KEY, streamer TEXT, broadcaster_id TEXT, url TEXT, title TEXT, views INTEGER, seconds REAL, created TEXT, ts REAL, game TEXT,
 video_id TEXT, vod_offset REAL, creator TEXT, thumb_url TEXT
);
CREATE INDEX IF NOT EXISTS clip_index_time ON clip_index(streamer, ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS usage (source_id TEXT, production_id TEXT, kind TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(source_id, production_id));
CREATE TABLE IF NOT EXISTS event (
 id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, ref TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS existing_video_sha256 ON existing_video(sha256);
CREATE INDEX IF NOT EXISTS src_state ON source(state);
CREATE INDEX IF NOT EXISTS prod_state ON production(state);
'''
NEW_COLUMNS = {
    'source': {'video_id': 'TEXT', 'vod_offset': 'REAL', 'game': 'TEXT', 'creator': 'TEXT', 'thumb_url': 'TEXT', 'sha256': 'TEXT', 'fp': 'BLOB',
               'score': 'REAL', 'score_json': 'TEXT', 'risk': 'TEXT', 'risk_json': 'TEXT', 'rating': 'INTEGER DEFAULT 0', 'allow_hold': 'INTEGER DEFAULT 0',
               'layout_json': 'TEXT', 'note': 'TEXT', 'tries': 'INTEGER DEFAULT 0', 'people': 'INTEGER DEFAULT 1', 'crowd_titles': 'TEXT'},
    'production': {'plan_json': 'TEXT', 'score': 'REAL', 'risk': 'TEXT', 'tags': 'TEXT', 'headline': 'TEXT', 'options_json': 'TEXT', 'qc_json': 'TEXT',
                   'review_json': 'TEXT', 'seconds': 'REAL', 'sha256': 'TEXT', 'updated': 'TEXT', 'upload_note': 'TEXT', 'publish_at': 'TEXT', 'live': 'TEXT', 'purged': 'INTEGER DEFAULT 0'},
    'existing_video': {'fp': 'BLOB', 'seconds': 'REAL'},
}
_ready = threading.Lock()
_migrated = False


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def migrate():
    """Additive only: adds columns, never drops or rewrites. Backs up first."""
    global _migrated
    with _ready:
        if _migrated:
            return
        fresh = not DB_PATH.exists()
        con = _connect()
        try:
            ver = con.execute('PRAGMA user_version').fetchone()[0]
            if not fresh and ver < DB_VERSION:
                backup_db('before_v05')
            con.executescript(SCHEMA)
            for table, cols in NEW_COLUMNS.items():
                have = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
                for c, t in cols.items():
                    if c not in have:
                        con.execute(f'ALTER TABLE {table} ADD COLUMN {c} {t}')
            if ver < DB_VERSION:
                con.execute(f'PRAGMA user_version={DB_VERSION}')
            # A crash mid-build must never strand a Short.
            con.execute("UPDATE production SET state='failed',error='Interrupted; press Build to retry' WHERE state='building'")
            con.commit()
        finally:
            con.close()
        _migrated = True
    c = settings()
    if CONF_PATH.exists() and int(c.get('settings_version') or 0) < 4:
        # One-time: adopt the requested 60-day / 50-clip discovery and the better local speech model.
        shutil.copy2(CONF_PATH, BACKUPS / 'settings_before_v04.json')
        if int(c.get('discover_days') or 0) < 60:
            c['discover_days'] = 60
        if int(c.get('max_clips_per_streamer') or 0) < 50:
            c['max_clips_per_streamer'] = 50
        if c.get('whisper_model') in ('tiny.en', '', None):
            c['whisper_model'] = 'auto'
        c['settings_version'] = 4
        save_settings(c)
    if CONF_PATH.exists() and int(c.get('settings_version') or 0) < 5:
        # Calmer, more human defaults after feedback on the v0.4 pilot. Only untouched values change.
        shutil.copy2(CONF_PATH, BACKUPS / 'settings_before_v05.json')
        for k, old, new in (('edit_energy', 1.0, .45), ('edit_teaser', True, False), ('edit_replay', True, False), ('edit_sfx_gain', .8, .35)):
            if c.get(k) == old:
                c[k] = new
        c['settings_version'] = 5
        save_settings(c)


def backup_db(label='auto', keep=12):
    """Consistent copy next to the data and a second one on the system drive."""
    if not DB_PATH.exists():
        return None
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    name = f'pipeline_{label}_{stamp}.db'
    dest = BACKUPS / name
    a = sqlite3.connect(DB_PATH)
    b = sqlite3.connect(dest)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    try:
        base = os.environ.get('LOCALAPPDATA')
        if base and 'FUNNYCOMM_DATA' not in os.environ:
            alt = Path(base) / 'FunnyComm' / 'db_backups'
            alt.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dest, alt / name)
            for old in sorted(alt.glob('pipeline_auto_*.db'))[:-keep]:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    for old in sorted(BACKUPS.glob('pipeline_auto_*.db'))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


def daily_backup():
    today = datetime.now().strftime('%Y%m%d')
    if not any(BACKUPS.glob(f'pipeline_auto_{today}_*.db')):
        return backup_db('auto')


@contextmanager
def db():
    migrate()
    con = _connect()
    try:
        with con:
            yield con
    finally:
        con.close()


def event(kind, ref='', detail=''):
    with db() as con:
        con.execute('INSERT INTO event(kind,ref,detail) VALUES(?,?,?)', (kind, ref, str(detail)[:600]))


def safe_slug(s, length=60):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', s)
    return re.sub(r'\s+', '_', s.strip(' ._'))[:length] or 'clip'


def log(msg, notify=print):
    notify(msg)


def source_rows(state=None):
    cols = ('id,streamer,source_kind,url,title,views,seconds,created,broadcaster_id,state,path,error,added,video_id,vod_offset,game,creator,thumb_url,'
            'sha256,score,score_json,risk,risk_json,rating,allow_hold,layout_json,note,tries,people,crowd_titles')
    with db() as con:
        return [dict(x) for x in con.execute(f'SELECT {cols} FROM source ' + ('WHERE state=? ' if state else '') + 'ORDER BY views DESC, created DESC', ([state] if state else []))]


def source(sid):
    with db() as con:
        r = con.execute('SELECT * FROM source WHERE id=?', (sid,)).fetchone()
        return dict(r) if r else None


def production_rows(state=None):
    with db() as con:
        return [dict(x) for x in con.execute('SELECT * FROM production ' + ('WHERE state=? ' if state else '') + 'ORDER BY added DESC', ([state] if state else []))]


def production(pid):
    with db() as con:
        r = con.execute('SELECT * FROM production WHERE id=?', (pid,)).fetchone()
        return dict(r) if r else None


def update_source(sid, **kwargs):
    with db() as con:
        con.execute('UPDATE source SET ' + ','.join(f'{k}=?' for k in kwargs) + ' WHERE id=?', (*kwargs.values(), sid))


def update_prod(pid, **kwargs):
    kwargs.setdefault('updated', datetime.now().isoformat(timespec='seconds'))
    with db() as con:
        con.execute('UPDATE production SET ' + ','.join(f'{k}=?' for k in kwargs) + ' WHERE id=?', (*kwargs.values(), pid))


# ------------------------------------------------------------ duplicate library
VIDEO_SUFFIXES = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v'}
TWITCH_URL_RE = re.compile(
    r'https?://(?:www\.|m\.)?twitch\.tv/[^\s\"<>]+/clip/([A-Za-z0-9_-]+)'
    r'|https?://clips\.twitch\.tv/([A-Za-z0-9_-]+)', re.I)
CLIP_NAME_RE = re.compile(r'(?:^|[_ .-])clip[_ .-]([A-Za-z0-9_-]{8,})(?:$)', re.I)


def skip_ids():
    with db() as con:
        return {r['id'] for r in con.execute('SELECT id FROM clip_skip')}


def normalize_twitch_id(value):
    value = str(value or '').strip()
    if not value:
        return None
    if value.startswith('clip:'):
        return value
    match = TWITCH_URL_RE.search(value)
    if match:
        return 'clip:' + next(x for x in match.groups() if x)
    return None      # a bare word could be a title; never treat it as an ID


def register_skipped_ids(items, reason='Already in library'):
    changed = 0
    with db() as con:
        for raw, path in items:
            clip_id = normalize_twitch_id(raw)
            if not clip_id:
                continue
            con.execute('INSERT OR IGNORE INTO clip_skip(id,path,reason) VALUES (?,?,?)', (clip_id, str(path or ''), reason))
            con.execute("UPDATE source SET state='existing',note=?,path=CASE WHEN ? <> '' THEN ? ELSE path END,error=NULL WHERE id=? AND state='discovered'",
                        (reason, str(path or ''), str(path or ''), clip_id))
            changed += 1
    return changed


def import_skip_urls(path):
    """Import a text/CSV/list of full Twitch clip URLs."""
    file = Path(path)
    contents = file.read_text(encoding='utf-8-sig', errors='replace')
    return register_skipped_ids(((m.group(0), str(file)) for m in TWITCH_URL_RE.finditer(contents)), reason='Imported user clip URL')


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as fh:
        for chunk in iter(lambda: fh.read(2 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _find_clip_id_for_file(path, known_ids):
    for suffix in ('.url', '.txt', '.json'):
        sidecar = path.with_suffix(suffix)
        if sidecar.is_file() and sidecar.stat().st_size < 100000:
            try:
                match = TWITCH_URL_RE.search(sidecar.read_text(encoding='utf-8-sig', errors='replace'))
                if match:
                    return 'clip:' + next(v for v in match.groups() if v)
            except OSError:
                pass
    match = CLIP_NAME_RE.search(path.stem)
    if match:
        return 'clip:' + match.group(1)
    normalized = path.stem.lower()
    for kid in known_ids:
        slug = kid[5:]
        if len(slug) >= 8 and re.search(r'(?<![A-Za-z0-9])' + re.escape(slug.lower()) + r'(?![A-Za-z0-9])', normalized):
            return kid
    return None


def scan_existing_folder(folder, notify=print, limit=2500):
    """Read-only inventory: never moves, renames or edits your files.

    v0.4 adds an audio fingerprint, so re-encoded, re-cropped and already-edited
    copies are recognised even when names and bytes are completely different.
    """
    from funnycomm import audio, media
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError('Select an existing directory containing video clips.')
    with db() as con:
        known = {r['id'] for r in con.execute("SELECT id FROM source WHERE source_kind='clip'")}
        prior = {r['path']: dict(r) for r in con.execute('SELECT path,size,modified_ns,sha256,clip_id,fp IS NOT NULL AS has_fp FROM existing_video')}
    candidates = []
    for path in folder.rglob('*'):
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and not path.name.endswith('.partial.mp4'):
            candidates.append(path)
            if len(candidates) >= limit:
                notify(f'Inventory capped at {limit} videos; choose a narrower folder to cover everything.')
                break
    inserted = failed = cached = 0
    ids = []
    for n, path in enumerate(candidates, 1):
        media.check_cancel()
        try:
            stat = path.stat()
            key = str(path)
            old = prior.get(key)
            if old and old['size'] == stat.st_size and old['modified_ns'] == stat.st_mtime_ns and old['sha256'] and old['has_fp']:
                clip_id = old['clip_id']
                cached += 1
            else:
                digest = file_digest(path)
                clip_id = _find_clip_id_for_file(path, known)
                try:
                    fp = audio.fingerprint(path)
                except Exception:
                    fp = None
                blob = fp.tobytes() if fp is not None and len(fp) else b''
                with db() as con:
                    con.execute("""INSERT INTO existing_video(path,size,modified_ns,sha256,clip_id,fp,seconds) VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(path) DO UPDATE SET size=excluded.size,modified_ns=excluded.modified_ns,sha256=excluded.sha256,
                        clip_id=excluded.clip_id,fp=excluded.fp,seconds=excluded.seconds""",
                                (key, stat.st_size, stat.st_mtime_ns, digest, clip_id, blob, (len(blob) // 4) * audio.FP_HOP))
            if clip_id:
                ids.append((clip_id, key))
            inserted += 1
            if n % 10 == 0:
                notify(f'Indexed {n}/{len(candidates)} existing videos...')
        except (OSError, PermissionError) as e:
            failed += 1
            notify(f'SCAN ERROR {path.name}: {str(e)[:130]}')
    matched = register_skipped_ids(ids, reason='Already present in local video library')
    moved = recheck_downloads(notify)
    result = {'files_indexed': inserted, 'clip_ids_identified': matched, 'cached': cached, 'failed_files': failed,
              'already_used_downloads': moved, 'folder': str(folder)}
    notify(f'EXISTING LIBRARY: {inserted} videos indexed, {matched} Twitch IDs matched, {moved} downloaded clip(s) recognised as already used, {failed} failures.')
    return result


def find_same_existing_video(path):
    digest = file_digest(path)
    with db() as con:
        for row in con.execute('SELECT path FROM existing_video WHERE sha256=?', (digest,)):
            same = Path(row['path'])
            if same.is_file() and same.resolve() != Path(path).resolve():
                return str(same)
    return None


def ensure_fp(sid, path):
    """Cached fingerprint of a source clip."""
    import numpy as np
    from funnycomm import audio
    with db() as con:
        r = con.execute('SELECT fp FROM source WHERE id=?', (sid,)).fetchone()
    if r and r['fp'] is not None:
        return np.frombuffer(r['fp'], np.uint32)
    fp = audio.fingerprint(path)
    update_source(sid, fp=fp.tobytes())
    return fp


def find_duplicate(sid, path):
    """Where this moment was already used: your old library, or another clip here.

    -> dict(where, kind 'library'|'source', seconds, share) or None
    """
    import numpy as np
    from funnycomm import audio
    fp = ensure_fp(sid, path)
    if len(fp) < 40:
        return None
    mine = len(fp) * audio.FP_HOP
    with db() as con:
        lib = con.execute('SELECT path,fp FROM existing_video WHERE fp IS NOT NULL AND length(fp)>160').fetchall()
        oth = con.execute("SELECT id,title,streamer,fp,state,views FROM source WHERE id<>? AND fp IS NOT NULL AND length(fp)>160 AND state IN ('downloaded','used')", (sid,)).fetchall()
    best = None
    for r in lib:
        m = audio.match(np.frombuffer(r['fp'], np.uint32), fp)
        if m and m['ber'] <= audio.SAME_BER and m['seconds'] >= min(.35 * mine, 9):
            d = {'kind': 'library', 'where': r['path'], 'seconds': m['seconds'], 'share': round(m['seconds'] / mine, 2), 'ber': m['ber']}
            if best is None or d['seconds'] > best['seconds']:
                best = d
    if best:
        return best
    for r in oth:
        m = audio.match(np.frombuffer(r['fp'], np.uint32), fp)
        if m and m['ber'] <= audio.SAME_BER and m['seconds'] >= min(.35 * mine, 9):
            return {'kind': 'source', 'where': r['id'], 'title': r['title'], 'state': r['state'], 'views': r['views'],
                    'seconds': m['seconds'], 'share': round(m['seconds'] / mine, 2), 'ber': m['ber']}
    return None


def recheck_downloads(notify=print):
    """Re-test waiting downloads against the library. Files stay where they are."""
    moved = 0
    rows = source_rows('downloaded')
    for s in rows:
        if not s.get('path') or not Path(s['path']).is_file() or (s.get('note') or '').startswith('Use anyway'):
            continue
        d = find_duplicate(s['id'], s['path'])
        if d and d['kind'] == 'library':
            update_source(s['id'], state='existing', note=f'Already used: {Path(d["where"]).name}')
            register_skipped_ids([(s['id'], d['where'])], reason='Same moment already used (audio match)')
            notify(f'ALREADY USED: {s["streamer"]} - {(s["title"] or "")[:40]} = {Path(d["where"]).name}')
            moved += 1
    rows = sorted(source_rows('downloaded'), key=lambda r: -r['views'])
    gone = set()
    for s in rows:
        if s['id'] in gone or not s.get('path') or not Path(s['path']).is_file() or (s.get('note') or '').startswith('Use anyway'):
            continue
        d = find_duplicate(s['id'], s['path'])
        if d and d['kind'] == 'source' and d['where'] not in gone and (d['state'] == 'used' or d['views'] >= s['views']):
            update_source(s['id'], state='existing', note=f'Same moment as "{(d.get("title") or "")[:40]}"')
            notify(f'SAME MOMENT: {s["streamer"]} - {(s["title"] or "")[:40]} overlaps "{(d.get("title") or "")[:40]}"')
            gone.add(s['id'])
            moved += 1
    return moved


def record_sources(items):
    excluded = skip_ids()
    with db() as con:
        for s in items:
            obj = {'video_id': None, 'vod_offset': None, 'game': None, 'creator': None, 'thumb_url': None, 'people': 1, 'crowd_titles': None, **s}
            obj['state'] = 'existing' if obj['id'] in excluded else 'discovered'
            con.execute('''INSERT INTO source (id,streamer,source_kind,url,title,views,seconds,created,broadcaster_id,state,video_id,vod_offset,game,creator,thumb_url,people,crowd_titles)
              VALUES (:id,:streamer,:source_kind,:url,:title,:views,:seconds,:created,:broadcaster_id,:state,:video_id,:vod_offset,:game,:creator,:thumb_url,:people,:crowd_titles)
              ON CONFLICT(id) DO UPDATE SET views=excluded.views,title=excluded.title,video_id=COALESCE(excluded.video_id,video_id),
              vod_offset=COALESCE(excluded.vod_offset,vod_offset),game=COALESCE(excluded.game,game),creator=COALESCE(excluded.creator,creator),
              thumb_url=COALESCE(excluded.thumb_url,thumb_url),people=MAX(COALESCE(excluded.people,1),COALESCE(people,1)),crowd_titles=COALESCE(excluded.crowd_titles,crowd_titles)''', obj)
            if obj['state'] == 'existing':
                con.execute("UPDATE source SET state='existing' WHERE id=? AND state='discovered'", (obj['id'],))
    return len(items)


# -------------------------------------------------------------------- Twitch
def vod_overlap(a, b):
    """Share of the shorter clip that covers the same VOD seconds as the other (vod_offset is where a clip ends)."""
    if not a.get('video_id') or a.get('video_id') != b.get('video_id') or a.get('vod_offset') is None or b.get('vod_offset') is None:
        return 0.0
    s = max(a['vod_offset'] - (a.get('seconds') or 0), b['vod_offset'] - (b.get('seconds') or 0))
    e = min(a['vod_offset'], b['vod_offset'])
    return max(0.0, e - s) / max(1.0, min(a.get('seconds') or 1, b.get('seconds') or 1))


class Twitch:
    def __init__(self, conf):
        self.conf = conf
        if not conf.get('twitch_client_id') or not conf.get('twitch_client_secret'):
            raise RuntimeError('Add your Twitch application Client ID and Client Secret in Settings.')
        self.clientid = conf['twitch_client_id']
        self.token = None
        self.games = {}
        self.session = requests.Session()

    def app_token(self):
        if self.token:
            return self.token
        r = self.session.post('https://id.twitch.tv/oauth2/token', data={
            'client_id': self.clientid, 'client_secret': self.conf['twitch_client_secret'], 'grant_type': 'client_credentials'}, timeout=30)
        if not r.ok:
            raise RuntimeError(f'Twitch rejected the Client ID/Secret (HTTP {r.status_code}). Check them in Settings.')
        self.token = r.json()['access_token']
        return self.token

    def get(self, endpoint, params=None, user=False):
        tok = self.user_token() if user else self.app_token()
        for attempt in range(4):
            try:
                r = self.session.get('https://api.twitch.tv/helix/' + endpoint,
                                     headers={'Client-Id': self.clientid, 'Authorization': 'Bearer ' + tok}, params=params, timeout=35)
            except requests.RequestException as e:
                if attempt == 3:
                    raise RuntimeError(f'Twitch could not be reached: {type(e).__name__}')
                time.sleep(1.5 * 2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < 3:
                    time.sleep(min(20, 1.5 * 2 ** attempt + random.random()))
                    continue
            if r.status_code == 401 and not user and attempt == 0:
                self.token = None
                tok = self.app_token()
                continue
            if r.status_code >= 400:
                raise RuntimeError(f'Twitch {endpoint} HTTP {r.status_code}: {r.text[:300]}')
            return r.json()
        raise RuntimeError(f'Twitch {endpoint} kept failing; try again later.')

    def user_token(self):
        tok = self.conf.get('twitch_user_token', '')
        if not tok:
            raise RuntimeError('Authorized clip downloads require a Twitch user OAuth token with editor:manage:clips or channel:manage:clips.')
        r = self.session.get('https://id.twitch.tv/oauth2/validate', headers={'Authorization': 'OAuth ' + tok}, timeout=20)
        if r.status_code == 200:
            scopes = set(r.json().get('scopes', []))
            if not ({'editor:manage:clips', 'channel:manage:clips'} & scopes):
                raise RuntimeError('Twitch user token lacks editor:manage:clips or channel:manage:clips scope.')
            return tok
        ref = self.conf.get('twitch_refresh_token', '')
        if not ref:
            raise RuntimeError('Twitch token expired. Connect Twitch again in Settings.')
        r = self.session.post('https://id.twitch.tv/oauth2/token', data={'grant_type': 'refresh_token', 'refresh_token': ref,
                              'client_id': self.clientid, 'client_secret': self.conf['twitch_client_secret']}, timeout=30)
        if not r.ok:
            raise RuntimeError(f'Could not refresh Twitch authorization (HTTP {r.status_code}).')
        out = r.json()
        c = settings()
        c['twitch_user_token'], c['twitch_refresh_token'] = out['access_token'], out.get('refresh_token', ref)
        save_settings(c)
        self.conf.update(twitch_user_token=c['twitch_user_token'], twitch_refresh_token=c['twitch_refresh_token'])
        return out['access_token']

    def broadcaster(self, login):
        data = self.get('users', {'login': login})['data']
        if not data:
            raise RuntimeError(f'Twitch profile not found: {login}')
        return data[0]

    def game_names(self, ids):
        need = [i for i in dict.fromkeys(ids) if i and i not in self.games]
        for k in range(0, len(need), 100):
            for g in self.get('games', [('id', i) for i in need[k:k + 100]])['data']:
                self.games[g['id']] = g['name']
        return self.games

    def _row(self, c, user, uid):
        return {'id': 'clip:' + c['id'], 'streamer': user, 'source_kind': 'clip', 'url': c['url'], 'title': c.get('title') or 'Twitch clip',
                'views': int(c.get('view_count', 0)), 'seconds': float(c.get('duration', 0)), 'created': c.get('created_at', ''), 'broadcaster_id': uid,
                'video_id': c.get('video_id') or None, 'vod_offset': c.get('vod_offset'), 'game': self.games.get(c.get('game_id'), ''),
                'creator': c.get('creator_name', ''), 'thumb_url': c.get('thumbnail_url', '')}

    def discover(self, login, days=60, max_clips=50, max_highlights=4, notify=lambda s: None):
        who = self.broadcaster(login)
        uid, user = who['id'], who['login']
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(days=max(1, int(days)))
        excluded = skip_ids()
        with db() as con:
            rows = [dict(r) for r in con.execute('SELECT id,state,video_id,vod_offset,seconds FROM source WHERE streamer=?', (user,))]
        done = [r for r in rows if r['state'] in ('downloaded', 'used', 'existing', 'skipped')]
        excluded.update(r['id'] for r in done)
        # The whole window in weekly slices: one 60-day query only surfaces the most viewed weeks.
        weeks, seen = [], {}
        window_end = now
        while window_end > start_time:
            window_start = max(start_time, window_end - timedelta(days=7))
            params = {'broadcaster_id': uid, 'first': 100, 'started_at': window_start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'ended_at': window_end.strftime('%Y-%m-%dT%H:%M:%SZ')}
            week = {}
            for _ in range(3):
                response = self.get('clips', params)
                for c in response.get('data', []):
                    if 'clip:' + c['id'] in excluded:
                        continue
                    old = seen.get(c['id'])
                    if old is None or int(c.get('view_count', 0)) > int(old.get('view_count', 0)):
                        seen[c['id']] = c
                        week[c['id']] = c
                cursor = response.get('pagination', {}).get('cursor')
                if not cursor:
                    break
                params['after'] = cursor
            weeks.append(sorted(week.values(), key=lambda c: -int(c.get('view_count', 0))))
            window_end = window_start
        self.game_names([c.get('game_id') for c in seen.values()])
        # Round-robin across weeks, best first, skipping re-clips of a moment already kept.
        picked, taken, skipped_same = [], list(done), 0
        for depth in range(max((len(w) for w in weeks), default=0)):
            for w in weeks:
                if depth < len(w) and len(picked) < int(max_clips):
                    row = self._row(w[depth], user, uid)
                    if any(vod_overlap(row, t) > .5 for t in taken):
                        skipped_same += 1
                        continue
                    picked.append(row)
                    taken.append(row)
        if skipped_same:
            notify(f'{user}: {skipped_same} re-clips of the same moment left out')
        result = sorted(picked, key=lambda r: -r['views'])
        res = self.get('videos', {'user_id': uid, 'type': 'highlight', 'first': min(100, max(1, int(max_highlights)) * 4), 'sort': 'time'})
        count = 0
        for c in res.get('data', []):
            if count >= int(max_highlights):
                break
            itemid = 'highlight:' + c['id']
            if itemid in excluded:
                continue
            try:
                created = datetime.fromisoformat(c.get('created_at', '').replace('Z', '+00:00'))
            except ValueError:
                continue
            if created < start_time:
                continue
            result.append({'id': itemid, 'streamer': user, 'source_kind': 'highlight', 'url': c['url'], 'title': c.get('title') or 'Twitch highlight',
                           'views': int(c.get('view_count', 0)), 'seconds': parse_duration(c.get('duration', '')), 'created': c.get('created_at', ''), 'broadcaster_id': uid})
            count += 1
        return result

    def refresh_metadata(self, notify=lambda s: None):
        """Fill game / VOD position / views for clips recorded by older versions."""
        with db() as con:
            ids = [r['id'][5:] for r in con.execute("SELECT id FROM source WHERE source_kind='clip' AND (game IS NULL OR video_id IS NULL AND vod_offset IS NULL) ORDER BY added DESC LIMIT 400")]
        n = 0
        for k in range(0, len(ids), 100):
            data = self.get('clips', [('id', i) for i in ids[k:k + 100]] + [('first', 100)])['data']
            self.game_names([c.get('game_id') for c in data])
            with db() as con:
                for c in data:
                    con.execute('UPDATE source SET views=MAX(views,?),video_id=?,vod_offset=?,game=?,creator=?,thumb_url=? WHERE id=?',
                                (int(c.get('view_count', 0)), c.get('video_id') or None, c.get('vod_offset'), self.games.get(c.get('game_id'), ''),
                                 c.get('creator_name', ''), c.get('thumbnail_url', ''), 'clip:' + c['id']))
                    n += 1
        if n:
            notify(f'Updated details for {n} known clips')
        return n

    def download_clip(self, item, destination):
        tok = self.user_token()
        editor = self.conf.get('twitch_editor_id') or self.session.get('https://id.twitch.tv/oauth2/validate', headers={'Authorization': 'OAuth ' + tok}, timeout=20).json()['user_id']
        res = self.get('clips/downloads', {'editor_id': editor, 'broadcaster_id': item['broadcaster_id'], 'clip_id': item['id'].split(':', 1)[1]}, user=True)
        arr = res.get('data', [])
        url = arr[0].get('landscape_download_url') if arr else None
        if not url:
            raise RuntimeError('Twitch did not provide authorized landscape media. Ask broadcaster for editor access or supply an authorized local file.')
        download_file(url, destination)
        return destination


def parse_duration(value):
    return sum(int(num) * factor for num, factor in zip_duration(value))


def zip_duration(value):
    for num, unit in re.findall(r'(\d+)([hms])', str(value)):
        yield num, {'h': 3600, 'm': 60, 's': 1}[unit]


def download_file(url, target):
    target = Path(target)
    tmp = target.with_suffix(target.suffix + '.partial')
    try:
        with requests.get(url, stream=True, timeout=(15, 60)) as resp:
            resp.raise_for_status()
            with tmp.open('wb') as f:
                for part in resp.iter_content(chunk_size=1024 * 1024):
                    if part:
                        f.write(part)
        if tmp.stat().st_size < 10_000:
            raise RuntimeError('Downloaded file looks too small to be valid video')
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def download_permitted_public_clip(item, destination):
    """Optional public Twitch clip download (NOT Twitch's official API).

    Only for publicly accessible clips the operator has rights to use. No login
    cookies, private API, subscription bypass or restricted-media workarounds.
    """
    if item.get('source_kind') != 'clip':
        raise RuntimeError('Public-clip download is only supported for Twitch CLIPS, not highlights/VODs.')
    url = item.get('url', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in ('www.twitch.tv', 'twitch.tv', 'clips.twitch.tv', 'm.twitch.tv'):
        raise RuntimeError('Source is not a standard public Twitch clip URL.')
    if parsed.hostname in ('www.twitch.tv', 'twitch.tv', 'm.twitch.tv') and '/clip/' not in parsed.path:
        raise RuntimeError('Expected a Twitch clip page URL, not a channel or VOD page.')
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError('Optional public downloader is not installed. Run Install_Public_Clip_Downloads.bat first.') from exc
    from funnycomm import media
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 10_000:
        return destination
    # Download to the system drive first; only a verified file reaches the data drive.
    with tempfile.TemporaryDirectory(prefix='fc_public_') as tdir:
        params = {'outtmpl': str(Path(tdir) / 'media.%(ext)s'), 'format': 'best[ext=mp4]/best', 'merge_output_format': 'mp4',
                  'postprocessors': [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}], 'noplaylist': True, 'quiet': True, 'no_warnings': True,
                  'retries': 3, 'fragment_retries': 3, 'socket_timeout': 30, 'ignoreerrors': False, 'noprogress': True}
        try:
            params['ffmpeg_location'] = str(Path(media.tool('ffmpeg')).parent)
        except media.MediaError:
            pass
        with YoutubeDL(params) as ydl:
            status = ydl.download([url])
        if status not in (0, None):
            raise RuntimeError('Public Twitch clip downloader reported a failure.')
        video = Path(tdir) / 'media.mp4'
        if not video.exists():
            raise RuntimeError('The downloader could not produce an MP4. Check FFmpeg and whether the clip is public and available.')
        media.verify_playable(video)
        media.place(video, destination)
    return destination


def download_order(rows):
    """Balanced queue: view velocity and total views per streamer, interleaved."""
    by = {}
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            age = max(1, (now - datetime.fromisoformat((r['created'] or '').replace('Z', '+00:00'))).days)
        except ValueError:
            age = 30
        r['_vel'] = (r['views'] or 0) / age
        by.setdefault(r['streamer'], []).append(r)
    lanes = []
    for rs in by.values():
        vw = sorted(x['views'] or 0 for x in rs)
        ve = sorted(x['_vel'] for x in rs)
        pp = sorted(x.get('people') or 1 for x in rs)
        rs.sort(key=lambda x: -(vw.index(x['views'] or 0) + ve.index(x['_vel']) + 1.6 * pp.index(x.get('people') or 1)))
        lanes.append(rs)
    out = []
    while any(lanes):
        for lane in lanes:
            if lane:
                out.append(lane.pop(0))
    return out


def free_gb(path=None):
    return shutil.disk_usage(path or DATA).free / 1e9


def drive_warnings(hours=24):
    """Recent controller/disk errors from the Windows log (read-only)."""
    if os.name != 'nt':
        return 0
    ps = ("$n=0;try{$n=(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName=@('disk','UASPStor');StartTime=(Get-Date).AddHours(-%d)} -EA Stop|"
          "?{$_.Id -in 7,11,51,129,153}|measure).Count}catch{};$n" % hours)
    try:
        p = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True, timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return int(p.stdout.strip() or 0)
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0


def download_new(conf, notify=print, progress=lambda a, b: None, only=None, allow_used=False):
    from funnycomm import media, speech
    if not conf.get('permission_confirmed'):
        raise RuntimeError('First confirm that you have permission to download AND republish all selected Twitch footage.')
    if free_gb() < 3:
        raise RuntimeError(f'Only {free_gb():.1f} GB free on the data drive. Free some space first.')
    tw = None
    rows = [r for r in source_rows('discovered') if (r.get('tries') or 0) < 4]
    if only:
        rows = [r for r in rows if r['id'] in only]
    pending = download_order(rows)[:max(1, int(conf.get('max_downloads_per_run', 12)))] if not only else rows
    successes = 0
    for n, item in enumerate(pending):
        media.check_cancel()
        progress(n, len(pending))
        local = SRC / f'{safe_slug(item["streamer"])}_{safe_slug(item["id"])}.mp4'
        # Two long slugs could collide once truncated; keep them apart.
        with db() as con:
            clash = con.execute('SELECT id FROM source WHERE path=? AND id<>?', (str(local), item['id'])).fetchone()
        if clash:
            local = local.with_name(local.stem[:60] + '_' + hashlib.sha256(item['id'].encode()).hexdigest()[:8] + '.mp4')
        try:
            if item['source_kind'] == 'clip':
                if speech.title_has_slur(item.get('title')) and not item.get('allow_hold'):
                    update_source(item['id'], risk='hold', note='Held: title says a slur is used')
                    notify(f'HELD {item["streamer"]} - "{item["title"][:40]}": title says a slur is used')
                    continue
                for attempt in range(3):
                    try:
                        if conf.get('permitted_public_clip_downloads'):
                            download_permitted_public_clip(item, local)
                        else:
                            tw = tw or Twitch(conf)
                            tw.download_clip(item, local)
                        break
                    except Exception as e:
                        if attempt == 2 or 'not installed' in str(e) or '404' in str(e):
                            raise
                        time.sleep(2 * 2 ** attempt)
            else:
                if not conf.get('allow_owner_highlight_download'):
                    continue
                if item['seconds'] > int(conf.get('max_highlight_seconds', 480)):
                    notify(f'SKIPPED long highlight: {item["seconds"] / 60:.0f} min > max allowed. Use clips or trim the source manually.')
                    continue
                if not conf.get('twitch_allow_yt_dlp_for_authorized'):
                    continue
                try:
                    from yt_dlp import YoutubeDL
                except ImportError:
                    raise RuntimeError('Run Install_Optional_Downloads.bat to enable permitted highlight downloads.')
                with YoutubeDL({'outtmpl': str(local), 'format': 'best[ext=mp4]/best', 'noplaylist': True, 'quiet': True, 'no_warnings': True, 'noprogress': True}) as ydl:
                    ydl.download([item['url']])
                if not local.exists():
                    raise RuntimeError('Highlight downloader did not create expected MP4.')
            match = None if allow_used else find_same_existing_video(local)
            if match:
                local.unlink(missing_ok=True)      # only the new copy; your library file is untouched
                register_skipped_ids([(item['id'], match)], reason='Exact duplicate already present')
                update_source(item['id'], state='existing', path=match, error=None, note=f'Identical to {Path(match).name}')
                notify(f'SKIPPED duplicate: {item["title"][:50]} (already in {Path(match).name})')
                continue
            update_source(item['id'], state='downloaded', path=str(local), error=None, sha256=file_digest(local))
            dup = None if allow_used else find_duplicate(item['id'], local)
            if dup:
                where = Path(dup['where']).name if dup['kind'] == 'library' else f'"{(dup.get("title") or "")[:40]}"'
                update_source(item['id'], state='existing', note=f'Same moment as {where}')
                notify(f'SKIPPED same moment: {item["streamer"]} - {item["title"][:40]} = {where}')
                continue
            notify(f'DOWNLOADED: {item["streamer"]} - {item["title"][:50]}')
            successes += 1
        except media.Cancelled:
            raise
        except Exception as e:
            update_source(item['id'], error=str(e)[:1200], tries=(item.get('tries') or 0) + 1)
            notify('FAILED download ' + item['id'] + ': ' + str(e)[:300])
    progress(len(pending), len(pending))
    return successes


def import_local(file, streamer='local'):
    file = Path(file)
    h = hashlib.sha256()
    with file.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    uid = 'local:' + h.hexdigest()[:22]
    with db() as con:
        prior = con.execute('SELECT state FROM source WHERE id=?', (uid,)).fetchone()
    if prior and prior['state'] in ('downloaded', 'used'):
        return uid + ' (already imported)'
    target = SRC / f'{safe_slug(streamer)}_{safe_slug(file.stem)}_{uid[-8:]}{file.suffix.lower()}'
    if not target.exists():
        from funnycomm import media
        media.place(file, target)
    row = {'id': uid, 'streamer': streamer, 'source_kind': 'local', 'url': 'Local import: ' + file.name,
           'title': file.stem.replace('_', ' '), 'views': 0, 'seconds': 0, 'created': '', 'broadcaster_id': ''}
    record_sources([row])
    update_source(uid, state='downloaded', path=str(target), sha256=h.hexdigest())
    return uid


# --------------------------------------------------------- legacy text helpers
def safe_flags(title, transcript=None):
    """Kept for compatibility; funnycomm.safety supersedes it."""
    flags = []
    if transcript is None:
        flags.append('Audio not transcribed: language and copyrighted music unverified')
    flags.append('Visual/audio policy and copyright require human review')
    return flags


def transcribe(path, model_size='auto'):
    from funnycomm import speech
    r = speech.transcribe(path, model_size)
    return (' '.join(x[2] for x in r['lines']) if r['ok'] else None), r['error']


# ------------------------------------------------------------------- YouTube
def yt_service(conf):
    """YouTube with the saved sign-in. Never opens a browser; connecting is a separate, visible step."""
    import pipeline_youtube
    return pipeline_youtube.service(conf)


def upload_body(conf, prod):
    """The exact request body. privacyStatus is always 'private'; omitting it would default to PUBLIC."""
    status = {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False, 'embeddable': True}
    body = {'snippet': {'title': prod['title'][:100], 'description': (prod['description'] or '')[:4900], 'categoryId': str(conf.get('youtube_category_id', '20')),
                        'defaultLanguage': 'en'}, 'status': status}
    try:
        tags = json.loads(prod.get('tags') or '[]')
    except ValueError:
        tags = []
    if tags:
        body['snippet']['tags'] = tags
    # A time the owner's approval put on this Short. It stays private until then.
    if prod.get('publish_at') and conf.get('posting_mode') == 'schedule' and prod.get('kind') != 'video':
        status['publishAt'] = prod['publish_at']
    if conf.get('paid_promotion') in ('yes', 'no'):
        body['paidProductPlacementDetails'] = {'hasPaidProductPlacement': conf['paid_promotion'] == 'yes'}
    return body


def _hm(v, default):
    try:
        h, m = str(v).split(':')
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def unix(s):
    return datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


def next_slots(conf, n, now=None):
    """The next free publish times, spread evenly over the owner's posting hours."""
    now = now or time.time()
    per = max(1, min(100, int(conf.get('posts_per_day') or 20)))
    a, z = _hm(conf.get('post_from'), 480), _hm(conf.get('post_until'), 1380)
    z = z if z > a else a + 720
    step = (z - a) / max(1, per - 1) if per > 1 else 0
    with db() as con:
        busy = [unix(r['publish_at']) for r in con.execute("SELECT publish_at FROM production WHERE publish_at IS NOT NULL AND state IN ('approved','uploaded_private')")]
    out, day = [], datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    gap = max(240, step * 60 * .45)
    for d in range(0, 400):
        base = (day + timedelta(days=d)).timestamp()
        left = per - sum(base <= t < base + 86400 for t in busy + out)
        for k in range(per):
            if left <= 0 or len(out) >= n:
                break
            t = base + (a + k * step) * 60
            if t < now + 25 * 60 or any(abs(t - q) < gap for q in busy + out):
                continue
            out.append(t)
            left -= 1
        if len(out) >= n:
            break
    return out


def assign_slot(conf, pid):
    """Give an approved Short its publish time."""
    p = production(pid)
    if conf.get('posting_mode') != 'schedule' or p['kind'] == 'video':
        return None
    if p.get('publish_at') and unix(p['publish_at']) > time.time() + 15 * 60:
        return p['publish_at']
    update_prod(pid, publish_at=None)
    t = next_slots(conf, 1)
    update_prod(pid, publish_at=utc(t[0]) if t else None)
    return utc(t[0]) if t else None


def upload_private(conf, prod, notify=print, confirm_retry=False, yt=None, wait_thumb=True, progress=None):
    if prod.get('youtube_id'):
        return prod['youtube_id']
    fresh = production(prod['id'])
    if not fresh or fresh['state'] != 'approved':
        raise RuntimeError('Local review approval is required before YouTube upload.')
    if fresh.get('youtube_id'):
        return fresh['youtube_id']
    if fresh.get('upload_note') == 'started' and not confirm_retry:
        raise RuntimeError('A previous upload of this was interrupted and may already exist. Check YouTube Studio, then use "Retry interrupted upload".')
    prod = fresh
    import pipeline_youtube as link
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    video, thumb = Path(prod['video']), Path(prod['thumbnail'])
    if not video.is_file() or not thumb.is_file():
        raise FileNotFoundError('Review video/thumbnail missing.')
    if prod.get('sha256') and file_digest(video) != prod['sha256']:
        raise RuntimeError('The video file changed since you approved it. Rebuild or review it again.')
    yt = yt or yt_service(conf)
    if conf.get('posting_mode') == 'schedule' and prod['kind'] != 'video':
        assign_slot(conf, prod['id'])
        prod = production(prod['id'])
    body = upload_body(conf, prod)
    assert body['status']['privacyStatus'] == 'private'
    chunk, size = 8 * 1024 * 1024, video.stat().st_size
    req = yt.videos().insert(part=','.join(body), body=body, media_body=MediaFileUpload(str(video), mimetype='video/mp4', resumable=True, chunksize=chunk))
    update_prod(prod['id'], upload_note='started')
    resp, errors = None, 0
    try:
        while resp is None:
            try:
                st, resp = req.next_chunk()
                errors = 0
                if st:
                    progress(st.progress()) if progress else notify(f'YouTube private upload: {st.progress() * 100:.0f}%')
            except HttpError as e:
                if e.resp.status in (500, 502, 503, 504) and errors < 5:
                    errors += 1
                    time.sleep(2 ** errors)
                    continue
                raise
            except (ConnectionError, TimeoutError, OSError):
                errors += 1
                if errors > 5:
                    raise RuntimeError('The internet connection dropped during the upload.')
                time.sleep(2 ** errors)
    except BaseException as e:
        # YouTube only creates the video when the last piece arrives. If that piece was never
        # sent, or Google refused outright, there is nothing to find in Studio.
        sent = getattr(req, 'resumable_progress', 0) or 0
        refused = isinstance(e, HttpError) and e.resp.status in (400, 401, 403)
        if refused or size - sent > chunk or getattr(req, 'resumable_uri', None) is None:
            update_prod(prod['id'], upload_note=None)
        if isinstance(e, HttpError):
            raise link.explain(e) from None
        raise
    vid = resp['id']
    update_prod(prod['id'], youtube_id=vid, state='uploaded_private', upload_note='done')
    event('upload_private', prod['id'], vid)
    notify('PRIVATE UPLOAD: https://studio.youtube.com/video/' + vid + '/edit')
    shown = (resp.get('status') or {}).get('privacyStatus')
    if shown and shown != 'private':
        event('privacy_alert', prod['id'], f'{vid} {shown}')
        raise link.StopUploads(f'YouTube reports this video as "{shown}" instead of private. Open YouTube Studio now and set it to Private: https://studio.youtube.com/video/{vid}/edit')
    if not wait_thumb:
        return vid
    for wait in (0, 20, 40, 60):
        time.sleep(wait)
        try:
            set_thumbnail(yt, vid, thumb)
            update_prod(prod['id'], thumbnail_set=1)
            notify('Thumbnail attached.')
            break
        except Exception as e:
            last = str(e)[:160]
    else:
        notify('THUMBNAIL pending (press "Retry thumbnails" later): ' + last)
    return vid


def set_thumbnail(yt, video_id, path):
    """Shorts accept custom covers only after processing has finished."""
    from googleapiclient.http import MediaFileUpload
    info = yt.videos().list(part='processingDetails', id=video_id).execute().get('items', [])
    status = (info[0].get('processingDetails', {}) if info else {}).get('processingStatus', 'unknown')
    if status != 'succeeded':
        raise RuntimeError('Video processing status: ' + status + '; thumbnail should be retried later.')
    mime = 'image/jpeg' if str(path).lower().endswith(('.jpg', '.jpeg')) else 'image/png'
    from googleapiclient.errors import HttpError
    import pipeline_youtube as link
    try:
        yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(path), mimetype=mime)).execute()
    except HttpError as e:
        if e.resp.status == 403 and link.reason_of(e) in ('forbidden', 'PERMISSION_DENIED', ''):
            raise link.StopUploads('YouTube only takes custom thumbnails from verified channels. Verify at https://www.youtube.com/verify, then press "Retry thumbnails".') from None
        raise link.explain(e) from None


def retry_thumbnails(conf, notify=print, yt=None):
    todo = [p for p in production_rows('uploaded_private') if not p['thumbnail_set'] and p['youtube_id']]
    if not todo:
        return 0
    import pipeline_youtube as link
    yt = yt or yt_service(conf)
    count = 0
    for p in todo:
        try:
            set_thumbnail(yt, p['youtube_id'], Path(p['thumbnail']))
            update_prod(p['id'], thumbnail_set=1)
            count += 1
            notify('Thumbnail attached: ' + p['youtube_id'])
        except (link.StopUploads, link.NotConnected):
            raise
        except Exception as e:
            notify('Still pending: ' + p['id'] + ': ' + str(e)[:150])
    return count


# ---------------------------------------------------------------- free space
def _inside(path, *roots):
    try:
        p = Path(path).resolve()
        return any(r.resolve() in p.parents for r in roots)
    except OSError:
        return False


def piece_cache():
    base = os.environ.get('LOCALAPPDATA')
    return (DATA / 'pieces') if os.environ.get('FUNNYCOMM_DATA') else Path(base) / 'FunnyComm' / 'pieces' if base else None


def _files(folder):
    return [f for f in folder.rglob('*') if f.is_file()] if folder and folder.is_dir() else []


def free_plan(uploaded_ok=None):
    """What one press of "Free up space" would delete. uploaded_ok: ids YouTube confirmed, or None to estimate."""
    plan = {'videos': [], 'clips': [], 'cache': [], 'prods': [], 'sources': [], 'waiting': 0}
    with db() as con:
        prods = [dict(r) for r in con.execute('SELECT id,state,youtube_id,sources_json,COALESCE(purged,0) AS purged FROM production')]
        use = {}
        for r in con.execute('SELECT source_id,production_id FROM usage'):
            use.setdefault(r['production_id'], set()).add(r['source_id'])
    ids = lambda p: set(json.loads(p['sources_json'] or '[]')) | use.get(p['id'], set())
    keep = set()
    for p in prods:
        if p['state'] in ('review', 'approved', 'building', 'failed') or (p['state'] == 'uploaded_private' and uploaded_ok is not None and p['id'] not in uploaded_ok and not p['purged']):
            keep |= ids(p)
    spent = set()
    for p in prods:
        if p['state'] != 'uploaded_private' or not p['youtube_id'] or (uploaded_ok is not None and p['id'] not in uploaded_ok and not p['purged']):
            plan['waiting'] += p['state'] == 'uploaded_private' and not p['purged']
            continue
        spent |= ids(p)
        if not p['purged']:
            folder = REVIEW / p['id']
            plan['videos'] += [f for f in _files(folder) if f.suffix.lower() in ('.mp4', '.wav', '.mkv') or re.match(r'thumbnail_\d', f.name)]
            plan['prods'].append(p['id'])
    for sid in spent - keep:
        s = source(sid)
        if s and s.get('path') and Path(s['path']).is_file() and _inside(s['path'], SRC, USED):
            plan['clips'].append(Path(s['path']))
            plan['sources'].append(sid)
    plan['cache'] = _files(piece_cache())
    plan['bytes'] = sum(f.stat().st_size for k in ('videos', 'clips', 'cache') for f in plan[k] if f.is_file())
    return plan


def free_space(uploaded_ok, notify=print):
    """Delete finished uploads (confirmed on YouTube), the clips used in them and temporary files."""
    plan = free_plan(set(uploaded_ok))
    freed, n = 0, 0
    for sid in plan['sources']:
        s = source(sid)
        try:
            ensure_fp(sid, s['path'])                    # remember the moment so it is never used twice
        except Exception as e:
            notify(f'Kept {Path(s["path"]).name[:40]}: {str(e)[:80]}')
            plan['clips'] = [f for f in plan['clips'] if f != Path(s['path'])]
            continue
        update_source(sid, state='used', note='Deleted to save space')
    for kind in ('videos', 'clips', 'cache'):
        for f in plan[kind]:
            if not (_inside(f, REVIEW, SRC, USED) or (piece_cache() and _inside(f, piece_cache()))):
                continue
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
                n += 1
            except OSError as e:
                notify(f'Could not delete {f.name}: {e}')
    for pid in plan['prods']:
        update_prod(pid, purged=1)
    event('free_space', '', f'{n} files, {freed / 1e9:.2f} GB')
    return {'files': n, 'bytes': freed, 'waiting': plan['waiting']}


# -------------------------------------------------------------- run exclusivity
class RunLock:
    """Stops the scheduled run and the app from working on the same clips at once."""

    def __init__(self, name='run.lock'):
        self.path = DATA / name
        self.fd = None

    def __enter__(self):
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f'{os.getpid()} {time.time():.0f}'.encode())
                return self
            except FileExistsError:
                if not self._stale():
                    raise RuntimeError('Another FunnyComm job is already running (the app or the daily task). Wait for it to finish.')
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise RuntimeError('Could not start: run.lock is busy.')

    def _stale(self):
        try:
            pid, at = self.path.read_text().split()
            if time.time() - float(at) > 6 * 3600:
                return True
            if os.name == 'nt':
                out = subprocess.run(['tasklist', '/FI', f'PID eq {int(pid)}', '/NH'], capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
                return 'python' not in out.lower()
            os.kill(int(pid), 0)
            return False
        except (ValueError, ProcessLookupError):
            return True
        except OSError:
            return False

    def __exit__(self, *a):
        try:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
