"""Connecting FunnyComm to the owner's YouTube channel, and uploading approved work as PRIVATE.

The Google sign-in file and the token stay on this PC. They are never printed, logged or packaged.
Nothing in this file can publish: every request body comes from pipeline_core.upload_body (private).
"""
from __future__ import annotations
import json, os, subprocess, time, webbrowser
from pathlib import Path
import pipeline_core as core

SCOPES = ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube']
TOKEN = core.DATA / 'youtube_token.json'
LINKS = {
    'personal': 'https://console.cloud.google.com/auth/overview/personal',
    'api': 'https://console.cloud.google.com/apis/library/youtube.googleapis.com',
    'security': 'https://myaccount.google.com/security',
    'verify': 'https://www.youtube.com/verify',
    'studio': 'https://studio.youtube.com',
}


class NotConnected(RuntimeError):
    """YouTube has to be connected (again) before anything can upload."""


class StopUploads(RuntimeError):
    """The reason affects every upload, so trying the rest would only repeat it."""


# ------------------------------------------------------------- sign-in file
def _home():
    if os.environ.get('FUNNYCOMM_DATA'):
        return core.DATA                                # throw-away test folders
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'FunnyComm' if base else core.DATA


def _private_file(path, text):
    """Write atomically and let only this Windows user read it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)
    try:
        if os.name == 'nt':
            subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'{os.environ.get("USERNAME", "Users")}:(R,W,D)'],
                           capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            os.chmod(path, 0o600)
    except OSError:
        pass


def check_client(path):
    """(usable, plain-language reason). Never returns any of the file's contents."""
    try:
        d = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except OSError:
        return False, 'That file could not be opened.'
    except ValueError:
        return False, 'That is not a Google sign-in file (it is not valid JSON).'
    if not isinstance(d, dict):
        return False, 'That is not a Google sign-in file.'
    i = d.get('installed')
    if isinstance(i, dict) and i.get('client_id') and i.get('client_secret') and i.get('token_uri') and i.get('auth_uri'):
        return True, 'Google sign-in file OK.'
    if 'installed' in d:
        return False, 'The sign-in file is incomplete. Create a new Desktop app client and download it again.'
    if 'web' in d:
        return False, 'That file is for a "Web application". In step 1 choose "Desktop app" and download that one.'
    if d.get('type') == 'service_account':
        return False, 'That is a service account key, which cannot upload to your channel. In step 1 choose "Desktop app".'
    if 'refresh_token' in d:
        return False, 'That is a saved sign-in, not the Google sign-in file. Use the file you downloaded in step 1.'
    return False, 'That is not a Google "Desktop app" sign-in file.'


def client_path(conf):
    for p in (conf.get('youtube_client_secret_path'), _home() / 'youtube_client.json', core.ROOT / 'youtube_client_secret.json'):
        if p and Path(p).is_file():
            return Path(p)
    return None


def find_client():
    """Newest usable sign-in file in the usual download places, or None."""
    home = Path.home()
    found = []
    for d in (home / 'Downloads', home / 'Desktop', home / 'Documents', home / 'OneDrive' / 'Downloads', home / 'OneDrive' / 'Desktop', home / 'OneDrive' / 'Documents'):
        try:
            found += [p for p in d.glob('client_secret*.json')]
        except OSError:
            pass
    for p in sorted(found, key=lambda q: -q.stat().st_mtime):
        if check_client(p)[0]:
            return p
    return None


def import_client(path):
    """Keep a private copy of the sign-in file, so clearing Downloads does not break uploads."""
    ok, why = check_client(path)
    if not ok:
        raise ValueError(why)
    new = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    old = client_path(core.settings())
    try:
        changed = not old or json.loads(old.read_text(encoding='utf-8-sig'))['installed']['client_id'] != new['installed']['client_id']
    except (OSError, ValueError, KeyError, TypeError):
        changed = True
    target = _home() / 'youtube_client.json'
    _private_file(target, json.dumps(new))
    c = core.settings()
    c['youtube_client_secret_path'] = str(target)
    core.save_settings(c)
    if changed:
        disconnect()                                    # a token belongs to the client that asked for it
    return target


def disconnect():
    """Forget the saved sign-in on this PC. Nothing on YouTube changes."""
    TOKEN.unlink(missing_ok=True)
    _remember(None)


# ------------------------------------------------------------------ sign-in
def _remember(ch):
    from funnycomm import index
    index.kv_set('youtube_channel', ch)


def status(conf):
    """What the app should show, without contacting Google."""
    from funnycomm import index
    p = client_path(conf)
    if not p:
        return {'ready': False, 'step': 'client', 'text': 'Not connected: no Google sign-in file yet.', 'channel': None}
    ok, why = check_client(p)
    if not ok:
        return {'ready': False, 'step': 'client', 'text': why, 'channel': None}
    if not TOKEN.is_file():
        return {'ready': False, 'step': 'signin', 'text': 'Sign-in file found. Next: sign in with Google.', 'channel': None}
    ch = index.kv_get('youtube_channel') or {}
    return {'ready': True, 'step': 'done', 'text': f'Connected to {ch["title"]}.' if ch.get('title') else 'Connected.', 'channel': ch or None}


def _load():
    from google.oauth2.credentials import Credentials
    try:
        d = json.loads(TOKEN.read_text(encoding='utf-8'))
        if d.get('scopes') and not set(SCOPES) <= set(d['scopes']):
            return None
        return Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    except (OSError, ValueError, TypeError):
        return None


class _Opener:
    """Opens the sign-in page and remembers its address, so it can be pasted into another browser profile."""
    url = ''

    def open(self, url, new=0, autoraise=True):
        _Opener.url = url
        return webbrowser.open(url, new=new, autoraise=autoraise)


def sign_in_link():
    """The address of the sign-in that is being waited for ('' when none is)."""
    return _Opener.url


def credentials(conf, interactive=False, timeout=600):
    """Valid credentials. Only `interactive` may open a browser, so an upload can never hang waiting for one."""
    client = client_path(conf)
    if not client:
        raise NotConnected('YouTube is not connected yet. Press "Connect YouTube" and follow the steps once.')
    ok, why = check_client(client)
    if not ok:
        raise NotConnected(why)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError, TransportError
    except ImportError:
        raise RuntimeError('YouTube Google API libraries not installed; run Install_Pipeline.bat.')
    creds = None if interactive else _load()
    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            raise NotConnected('Google no longer accepts the saved sign-in. Press "Connect YouTube" and sign in again. (If this happens every week, the sign-in file is from a "Testing" app; make one in Personal mode, step 1.)')
        except TransportError:
            raise RuntimeError('Could not reach Google. Check the internet connection and try again.')
    if not creds or not creds.valid:
        if not interactive:
            raise NotConnected('YouTube needs you to sign in. Press "Connect YouTube", then "Sign in with Google".')
        os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'  # an unticked box is explained below instead of crashing
        flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
        webbrowser.register('funnycomm-signin', None, _Opener())
        try:
            creds = flow.run_local_server(port=0, open_browser=True, authorization_prompt_message='', timeout_seconds=timeout, prompt='select_account consent', browser='funnycomm-signin',
                                          success_message='YouTube is connected. Close this tab and go back to FunnyComm.')
        except Exception as e:
            text = f'{type(e).__name__} {e}'
            if 'Timeout' in text:
                raise NotConnected('The Google sign-in was not finished within ten minutes. Press "Sign in with Google" to try again.')
            if 'access_denied' in text:
                raise NotConnected('Google sign-in was cancelled or blocked. Use the same Google account that created the sign-in file.')
            if 'invalid_client' in text or 'deleted_client' in text:
                raise NotConnected('Google does not recognise the sign-in file. Make a new one in step 1 and use that file.')
            raise NotConnected(f'Google sign-in did not finish ({type(e).__name__}). Try "Sign in with Google" again.')
        finally:
            _Opener.url = ''
        granted = set(getattr(creds, 'granted_scopes', None) or creds.scopes or [])
        if not set(SCOPES) <= granted:
            raise NotConnected('One permission box was left unticked. Press "Sign in with Google" again and tick every box.')
    _private_file(TOKEN, creds.to_json())
    return creds


def service(conf, interactive=False):
    creds = credentials(conf, interactive)
    from googleapiclient.discovery import build
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)


def channel(yt):
    from googleapiclient.errors import HttpError
    try:
        items = yt.channels().list(part='snippet,status', mine=True).execute().get('items') or []
    except HttpError as e:
        raise explain(e)
    if not items:
        raise NotConnected('That Google account has no YouTube channel. Sign in again and pick the account or brand channel you post from.')
    it = items[0]
    return {'id': it['id'], 'title': it['snippet'].get('title') or 'your channel', 'long': (it.get('status') or {}).get('longUploadsStatus')}


def sign_in(conf):
    """Open Google in the browser, then confirm which channel answered."""
    yt = service(conf, interactive=True)
    try:
        ch = channel(yt)
    except Exception:
        TOKEN.unlink(missing_ok=True)
        raise
    _remember(ch)
    core.event('youtube_connected', ch['id'], ch['title'])
    return ch


def test(conf):
    """Prove the connection works. Reads the channel name only."""
    ch = channel(service(conf))
    _remember(ch)
    return ch


# ------------------------------------------------------------------- errors
_REASONS = {
    'accessNotConfigured': (True, 'The YouTube Data API is not switched on for your Google project. Open "Connect YouTube", step 2, choose your project at the top, press Enable, wait two minutes and try again.'),
    'SERVICE_DISABLED': (True, 'The YouTube Data API is not switched on for your Google project. Open "Connect YouTube", step 2, choose your project at the top, press Enable, wait two minutes and try again.'),
    'youtubeSignupRequired': (True, 'That Google account has no YouTube channel. Press "Connect YouTube" and sign in with the account you post from.'),
    'uploadLimitExceeded': (True, 'YouTube will not accept more uploads from this channel right now. Try the rest tomorrow.'),
    'quotaExceeded': (True, 'The daily YouTube API limit is used up. It resets at midnight Pacific time; upload the rest then.'),
    'dailyLimitExceeded': (True, 'The daily YouTube API limit is used up. It resets at midnight Pacific time; upload the rest then.'),
    'rateLimitExceeded': (True, 'YouTube asked to slow down. Wait a few minutes and try the rest.'),
    'uploadRateLimitExceeded': (True, 'YouTube asked to slow down with thumbnails. Try "Retry thumbnails" later.'),
    'authError': (True, 'Google no longer accepts the saved sign-in. Press "Connect YouTube" and sign in again.'),
    'insufficientPermissions': (True, 'The sign-in is missing a permission. Press "Connect YouTube", sign in again and tick every box.'),
    'accountSuspended': (True, 'YouTube says this account is suspended.'),
    'channelSuspended': (True, 'YouTube says this channel is suspended.'),
    'invalidTitle': (False, 'YouTube did not accept the title. Edit it in the review screen.'),
    'invalidDescription': (False, 'YouTube did not accept the description. Edit it in the review screen.'),
    'invalidTags': (False, 'YouTube did not accept the tags.'),
    'invalidCategoryId': (True, 'The YouTube category number in Settings is not valid. Use 20, 23 or 24.'),
}


def reason_of(e):
    """The machine reason in a Google error, e.g. 'quotaExceeded'."""
    try:
        err = json.loads(e.content.decode('utf-8', 'replace'))['error']
    except (ValueError, KeyError, AttributeError, TypeError):
        return ''
    for x in err.get('details') or []:
        if isinstance(x, dict) and x.get('reason') in _REASONS:
            return x['reason']
    for x in err.get('errors') or []:
        if isinstance(x, dict) and x.get('reason'):
            return x['reason']
    return err.get('status') or ''


def explain(e):
    """A Google error as an exception with words a person can act on."""
    code, why = getattr(e.resp, 'status', '?'), reason_of(e)
    if why in _REASONS:
        fatal, text = _REASONS[why]
        return (StopUploads if fatal else RuntimeError)(text)
    if str(code) == '401':
        return StopUploads(_REASONS['authError'][1])
    return RuntimeError(f'YouTube refused the request (HTTP {code}{", " + why if why else ""}).')


# ------------------------------------------------------------------- batch
def upload_batch(conf, todo, notify=print, progress=lambda a, b, m='': None, cancel=lambda: None, thumb_minutes=8):
    """Upload every approved item as PRIVATE. Returns {'done': [(title, id)], 'failed': [(title, why)], 'stopped': why, 'channel': name, 'thumbs': n}."""
    out = {'done': [], 'failed': [], 'stopped': '', 'channel': '', 'thumbs': 0}
    yt = service(conf)                                  # NotConnected surfaces before anything starts
    ch = channel(yt)
    _remember(ch)
    out['channel'] = ch['title']
    notify(f'YouTube channel: {ch["title"]}')
    for n, p in enumerate(todo):
        cancel()
        title = p['title'] or p['id']

        def step(f, n=n, title=title):
            cancel()
            progress(n + f, len(todo), f'Uploading {n + 1} of {len(todo)} privately: "{title[:44]}" {f * 100:.0f}%')
        progress(n, len(todo), f'Uploading {n + 1} of {len(todo)} privately: "{title[:44]}"')
        try:
            vid = core.upload_private(conf, p, notify, yt=yt, wait_thumb=False, progress=step)
            out['done'].append((title, vid))
        except (StopUploads, NotConnected) as e:
            out['failed'].append((title, str(e)))
            out['stopped'] = str(e)
            notify(f'UPLOAD ERROR {title[:60]}: {e}')
            break
        except Exception as e:
            if type(e).__name__ == 'Cancelled':
                raise
            out['failed'].append((title, str(e)[:300]))
            notify(f'UPLOAD ERROR {title[:60]}: {str(e)[:300]}')
    # Covers are accepted once YouTube has finished processing.
    if out['done']:
        end = time.time() + thumb_minutes * 60
        while True:
            cancel()
            progress(len(todo), len(todo), 'Uploaded. Waiting for YouTube to process, then setting thumbnails...')
            left = [p for p in core.production_rows('uploaded_private') if not p['thumbnail_set'] and p['youtube_id']]
            if not left:
                break
            try:
                out['thumbs'] += core.retry_thumbnails(conf, lambda m: notify(m) if not m.startswith('Still pending') else None, yt=yt)
            except StopUploads as e:
                notify('THUMBNAILS: ' + str(e))
                break
            if time.time() > end or not [p for p in core.production_rows('uploaded_private') if not p['thumbnail_set'] and p['youtube_id']]:
                break
            for _ in range(30):
                cancel()
                time.sleep(1 if thumb_minutes else 0)
    return out


def summary(out, total):
    lines = [f'{len(out["done"])} of {total} uploaded as PRIVATE to {out["channel"]}.']
    if out['done']:
        lines += [''] + ['✓ ' + t[:70] for t, _ in out['done'][:12]]
    if out['failed']:
        lines += ['', 'Not uploaded:'] + [f'✗ {t[:60]}\n    {w}' for t, w in out['failed'][:6]]
    left = total - len(out['done']) - len(out['failed'])
    if out['stopped'] and left:
        lines += ['', f'{left} more not tried. They stay approved; press the upload button again later.']
    if out['done']:
        n = len([1 for p in core.production_rows('uploaded_private') if not p['thumbnail_set']])
        lines += ['', f'{n} thumbnail(s) still waiting for YouTube to finish processing. Press "Retry thumbnails" in a few minutes.' if n else 'All thumbnails are set.',
                  'Publish them yourself in YouTube Studio when you are ready.']
    return '\n'.join(lines)



# ------------------------------------------------------------ automatic poster
def sync(conf, yt=None, notify=lambda m: None):
    """Ask YouTube how the uploads are doing. Returns ids that are processed. Costs 1 unit per 50 videos."""
    yt = yt or service(conf)
    from funnycomm import index
    rows = [p for p in core.production_rows('uploaded_private') if p['youtube_id']]
    ok, trouble = set(), []
    views = dict(index.kv_get('views') or {})
    for k in range(0, len(rows), 50):
        part = rows[k:k + 50]
        try:
            got = {i['id']: i for i in yt.videos().list(part='status,statistics', id=','.join(p['youtube_id'] for p in part)).execute().get('items', [])}
        except Exception as e:
            raise explain(e) if hasattr(e, 'resp') else e
        items = {k_: v['status'] for k_, v in got.items()}
        for p in part:
            if p['youtube_id'] in got:
                views[p['id']] = int((got[p['youtube_id']].get('statistics') or {}).get('viewCount') or 0)
            st = items.get(p['youtube_id'])
            if st is None:
                if (p.get('live') or '') not in ('removed',):
                    core.update_prod(p['id'], live='removed')
                    trouble.append((p['title'], 'is no longer on YouTube'))
                continue
            up, pv = st.get('uploadStatus'), st.get('privacyStatus')
            state = 'rejected' if up in ('rejected', 'failed') else 'public' if pv == 'public' else 'scheduled' if st.get('publishAt') else pv or 'private'
            if state == 'rejected' and p.get('live') != 'rejected':
                trouble.append((p['title'], 'was rejected by YouTube: ' + str(st.get('rejectionReason') or st.get('failureReason') or up)))
            if state != p.get('live'):
                core.update_prod(p['id'], live=state)
            if up == 'processed':
                ok.add(p['id'])
    index.kv_set('views', views)
    for t, why in trouble:
        core.event('youtube_trouble', '', f'{t}: {why}')
        notify(f'ERROR: "{t}" {why}')
    if trouble:
        pause(f'"{trouble[0][0][:50]}" {trouble[0][1]}. Posting is paused so nothing else goes out until you look.', 10 * 365 * 24)
    return ok


def pause(why, hours):
    from funnycomm import index
    index.kv_set('post_pause', {'why': why, 'until': time.time() + hours * 3600})


def paused():
    from funnycomm import index
    p = index.kv_get('post_pause') or {}
    return p if p.get('until', 0) > time.time() else None


def resume():
    from funnycomm import index
    index.kv_set('post_pause', {})


def queue(conf):
    """Approved Shorts whose turn to upload has come (publish time within the next 30 hours)."""
    out = []
    for p in core.production_rows('approved'):
        if p['kind'] == 'video' or p.get('upload_note') == 'started':
            continue
        if conf.get('posting_mode') == 'schedule':
            core.assign_slot(conf, p['id'])
            p = core.production(p['id'])
            if not p.get('publish_at') or core.unix(p['publish_at']) > time.time() + 30 * 3600:
                continue
        out.append(p)
    return sorted(out, key=lambda p: p.get('publish_at') or '')


def post_next(conf, notify=lambda m: None, progress=lambda m: None, state={}):
    """One quiet step of the background poster. Returns True when it did something."""
    if not conf.get('auto_post', True) or not status(conf)['ready']:
        return False
    if paused():
        return False
    with core.RunLock('upload.lock'):
        now = time.time()
        todo = queue(conf)
        need_sync = now - state.get('sync', 0) > 30 * 60 and any(p['youtube_id'] for p in core.production_rows('uploaded_private'))
        need_thumb = now - state.get('thumb', 0) > 150 and any(not p['thumbnail_set'] and p['youtube_id'] for p in core.production_rows('uploaded_private'))
        if not (todo or need_sync or need_thumb):
            return False
        try:
            yt = service(conf)
            if need_sync:
                state['sync'] = now
                sync(conf, yt, notify)
                if paused():
                    return True
            if need_thumb:
                state['thumb'] = now
                core.retry_thumbnails(conf, lambda m: None, yt=yt)
            if todo:
                with core.db() as con:
                    day = con.execute("SELECT COUNT(*) FROM event WHERE kind='upload_private' AND at > datetime('now','-1 day')").fetchone()[0]
                if day >= 95:
                    return False
                p = todo[0]
                core.upload_private(conf, p, notify, yt=yt, wait_thumb=False, progress=lambda f: progress(f'Uploading "{p["title"][:44]}" {f * 100:.0f}%'))
                q = core.production(p['id'])
                if q.get('publish_at') and conf.get('posting_mode') == 'schedule':
                    notify(f'READY: "{p["title"][:50]}" will go public {time.strftime("%a %H:%M", time.localtime(core.unix(q["publish_at"])))}')
                progress('')
            return True
        except StopUploads as e:
            limit = 'uploads' in str(e) and 'tomorrow' in str(e) or 'limit' in str(e)
            pause(str(e), 12 if limit else 24)
            notify('NOTE: posting paused. ' + str(e))
        except NotConnected as e:
            pause(str(e), 1)
            notify('ERROR: ' + str(e))
        except RuntimeError as e:
            state['sync'] = state['thumb'] = now
            notify('UPLOAD ERROR ' + str(e)[:200])
            pause(str(e)[:200], .25)
        progress('')
        return True


def outlook(conf):
    """Numbers for the home screen, without contacting Google."""
    now = time.time()
    rows = [p for p in core.production_rows('uploaded_private') + core.production_rows('approved') if p.get('publish_at')]
    times = sorted(core.unix(p['publish_at']) for p in rows)
    nxt = next((t for t in times if t > now), None)
    day = time.localtime(now)[:3]
    return {'next': nxt, 'ahead': sum(t > now for t in times), 'today': sum(time.localtime(t)[:3] == day for t in times), 'paused': paused()}


def free_space(conf, notify=print):
    """Confirm on YouTube first, then delete local copies."""
    ok = sync(conf, None, notify)
    return core.free_space(ok, notify)
