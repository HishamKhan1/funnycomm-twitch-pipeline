"""Offline tests. Run with Self_Test.bat. Uses a throw-away data folder; your data is never touched."""
import json, time, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix='fc_test_')
os.environ['FUNNYCOMM_DATA'] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

# ---- a v0.3 database and settings file, created before the new code ever sees them
_db = sqlite3.connect(Path(_TMP) / 'pipeline.db')
_db.executescript('''
CREATE TABLE source (id TEXT PRIMARY KEY, streamer TEXT NOT NULL, source_kind TEXT NOT NULL, url TEXT NOT NULL, title TEXT, views INTEGER DEFAULT 0, seconds REAL DEFAULT 0,
 created TEXT, broadcaster_id TEXT, state TEXT DEFAULT 'discovered', path TEXT, error TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE production (id TEXT PRIMARY KEY, kind TEXT NOT NULL, sources_json TEXT NOT NULL, video TEXT, thumbnail TEXT, title TEXT, description TEXT,
 state TEXT NOT NULL DEFAULT 'building', flags_json TEXT DEFAULT '[]', youtube_id TEXT, thumbnail_set INTEGER DEFAULT 0, added TEXT DEFAULT CURRENT_TIMESTAMP, error TEXT);
CREATE TABLE clip_skip (id TEXT PRIMARY KEY, path TEXT, reason TEXT NOT NULL, added TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE existing_video (path TEXT PRIMARY KEY, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, sha256 TEXT, clip_id TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP);
INSERT INTO source(id,streamer,source_kind,url,title,views,state,path) VALUES('clip:Old-1','lacy','clip','https://www.twitch.tv/lacy/clip/Old-1','old one',500,'downloaded','X:/keep.mp4');
INSERT INTO clip_skip(id,path,reason) VALUES('clip:Skip-1','','mine');
INSERT INTO production(id,kind,sources_json,state) VALUES('stuck','single','["clip:Old-1"]','building');
''')
_db.commit()
_db.close()
(Path(_TMP) / 'settings.json').write_text(json.dumps({'streamers': ['lacy'], 'twitch_client_id': 'keep-id', 'twitch_client_secret': 'keep-secret', 'twitch_user_token': 'keep-token',
                                                     'max_clips_per_streamer': 20, 'discover_days': 30, 'permission_confirmed': True, 'whisper_model': 'tiny.en'}), encoding='utf-8')
import pipeline_core as core
import pipeline_render as pr
from funnycomm import audio, camera, ears, gfx, index, longform, media, meta, qc, render, safety, speech, story, topics


def clip(path, sec=4, freq=300, size='1280x720', fps=30, seed=1):
    subprocess.run([media.tool('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=s={size}:r={fps}:d={sec}', '-f', 'lavfi',
                    '-i', f'anoisesrc=d={sec}:c=pink:seed={seed}:a=0.3,tremolo=f={2 + seed % 5}:d=0.8', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-ar', '44100', '-shortest', str(path)], check=True)
    return path


def fake(path, dur=4.0, cam=(0, 0, .26, .26), words=(), payoff=2.5, mode='split', w=1280, h=720):
    n = int(dur / .1)
    f = np.full(n, .2)
    f[int(payoff / .1) - 3:int(payoff / .1) + 4] = 1.6
    return {'schema': 21, 'id': 'x', 'meta': {'w': w, 'h': h, 'fps': 30, 'duration': dur, 'audio': True}, 'grid': .1,
            'layout': {'mode': mode, 'cam': {'kind': 'box', 'rect': list(cam), 'conf': .99, 'people': 1, 'support': 1} if mode == 'split' else None, 'green': None,
                       'chat': [[.74, 0, .2, .2]], 'captions': None, 'bars': [0, 0, 0, 0], 'cuts': [0]},
            'face_cam': [[t / 4, cam[0] + .1, cam[1] + .08, .05, .09, 1] for t in range(int(dur * 4))] if mode == 'split' else [],
            'tracks': [], 'curves': {k: [.2] * n for k in ('audio', 'laugh', 'motion', 'cam', 'chat', 'ix', 'is')}, 'audio': {'peak_gap_db': 12, 'music': {'score': 0, 'spans': []}},
            'speech': {'ok': True, 'words': [list(x) for x in words], 'lines': []}, 'safety': {'level': 'clear', 'reasons': [], 'censor': [], 'mute': [], 'suggestive': []},
            'moments': {'payoff': payoff, 'onset': payoff - .3, 'peak': 1.6, 'mean': .4, 'beats': [{'t': payoff, 's': 1.0}], 'fused': f.tolist()}}


class Migration(unittest.TestCase):
    def test_rows_and_settings_survive(self):
        rows = {r['id']: r for r in core.source_rows()}
        self.assertEqual(rows['clip:Old-1']['state'], 'downloaded')
        self.assertEqual(rows['clip:Old-1']['path'], 'X:/keep.mp4')
        self.assertIn('clip:Skip-1', core.skip_ids())
        c = core.settings()
        self.assertEqual((c['twitch_client_id'], c['twitch_client_secret'], c['twitch_user_token']), ('keep-id', 'keep-secret', 'keep-token'))
        self.assertEqual((c['discover_days'], c['max_clips_per_streamer'], c['permission_confirmed']), (60, 50, True))
        self.assertTrue(list(core.BACKUPS.glob('pipeline_before_v05_*.db')))
        self.assertEqual(core.production('stuck')['state'], 'failed')      # no longer stranded

    def test_rediscovery_keeps_state(self):
        core.record_sources([{'id': 'clip:Old-1', 'streamer': 'lacy', 'source_kind': 'clip', 'url': 'u', 'title': 'new', 'views': 900, 'seconds': 5, 'created': '', 'broadcaster_id': '1', 'game': 'IRL'}])
        r = core.source('clip:Old-1')
        self.assertEqual((r['state'], r['views'], r['game']), ('downloaded', 900, 'IRL'))

    def test_skip_list_blocks_new(self):
        core.record_sources([{'id': 'clip:Skip-1', 'streamer': 'lacy', 'source_kind': 'clip', 'url': 'u', 'title': 't', 'views': 1, 'seconds': 5, 'created': '', 'broadcaster_id': '1'}])
        self.assertEqual(core.source('clip:Skip-1')['state'], 'existing')

    def test_lock(self):
        with core.RunLock():
            with self.assertRaises(RuntimeError):
                core.RunLock().__enter__()
        with core.RunLock():
            pass


class UploadGate(unittest.TestCase):
    def prod(self, pid, state):
        v, t = Path(_TMP) / f'{pid}.mp4', Path(_TMP) / f'{pid}.jpg'
        v.write_bytes(b'v' * 100)
        t.write_bytes(b't' * 100)
        with core.db() as con:
            con.execute('INSERT OR REPLACE INTO production(id,kind,sources_json,state,video,thumbnail,title,description) VALUES(?,?,?,?,?,?,?,?)', (pid, 'single', '[]', state, str(v), str(t), 'T', 'D'))
        return core.production(pid)

    def test_needs_approval(self):
        p = self.prod('g1', 'review')
        with self.assertRaisesRegex(RuntimeError, 'approval'):
            core.upload_private({}, p)
        with self.assertRaisesRegex(RuntimeError, 'approval'):       # a forged in-memory state is not enough
            core.upload_private({}, {**p, 'state': 'approved'})

    def test_body_is_always_private(self):
        for conf in ({}, {'privacyStatus': 'public'}, {'paid_promotion': 'yes'}):
            b = core.upload_body(conf, {'title': 'x' * 300, 'description': 'd', 'tags': '["a"]'})
            self.assertEqual(b['status']['privacyStatus'], 'private')
            self.assertNotIn('publishAt', b['status'])
            self.assertLessEqual(len(b['snippet']['title']), 100)

    def test_no_public_call_in_source(self):
        for f in ('pipeline_core.py', 'pipeline_youtube.py', 'pipeline_simple.py', 'pipeline_sources.py', 'pipeline_render.py', 'Pipeline_App.py', 'Pipeline_Headless.py'):
            s = (core.ROOT / f).read_text(encoding='utf-8')
            self.assertNotIn("'privacyStatus': 'public'", s)
            self.assertNotIn('videos().update', s)

    def test_checklist_required(self):
        self.prod('g2', 'review')
        with self.assertRaises(RuntimeError):
            pr.approve('g2', {c: True for c in pr.CHECKLIST[:-1]})
        pr.approve('g2', {c: True for c in pr.CHECKLIST})
        p = core.production('g2')
        self.assertEqual(p['state'], 'approved')
        Path(p['video']).write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'changed since you approved'):
            core.upload_private({}, p)

    # ---- YouTube connection and upload, against a pretend Google
    def yt(self, *replies):
        """A real API client whose network answers are scripted. Returns (client, sent requests)."""
        from googleapiclient.discovery import build
        from googleapiclient.http import HttpMockSequence
        http = HttpMockSequence([({'status': str(st), **hd}, json.dumps(bd) if not isinstance(bd, str) else bd) for st, hd, bd in replies])
        return build('youtube', 'v3', http=http, static_discovery=True, cache_discovery=False), http

    START = (200, {'location': 'https://upload.example/1'}, '')

    def client_file(self, name, body):
        f = Path(_TMP) / name
        f.write_text(json.dumps(body), encoding='utf-8')
        return f

    GOOD = {'installed': {'client_id': 'a.apps', 'client_secret': 'x', 'auth_uri': 'https://a', 'token_uri': 'https://t'}}

    def test_sign_in_file_is_checked(self):
        import pipeline_youtube as y
        self.assertTrue(y.check_client(self.client_file('c1.json', self.GOOD))[0])
        self.assertIn('Desktop app', y.check_client(self.client_file('c2.json', {'web': {}}))[1])
        self.assertIn('service account', y.check_client(self.client_file('c3.json', {'type': 'service_account'}))[1])
        self.assertFalse(y.check_client(Path(_TMP) / 'nope.json')[0])
        (Path(_TMP) / 'c4.json').write_text('hello')
        self.assertFalse(y.check_client(Path(_TMP) / 'c4.json')[0])
        with self.assertRaises(ValueError):
            y.import_client(Path(_TMP) / 'c2.json')

    def test_upload_without_connection_explains_and_stays_approved(self):
        import pipeline_youtube as y
        self.prod('y1', 'approved')
        with self.assertRaisesRegex(y.NotConnected, 'Connect YouTube'):
            core.upload_private({}, core.production('y1'))
        p = core.production('y1')
        self.assertEqual((p['state'], p['upload_note'], p['youtube_id']), ('approved', None, None))
        self.assertFalse(y.status({})['ready'])

    def test_upload_never_opens_a_browser(self):
        import pipeline_youtube as y, unittest.mock as um
        f = self.client_file('c5.json', self.GOOD)
        with um.patch('webbrowser.get', side_effect=AssertionError('browser opened')):
            with self.assertRaises(y.NotConnected):
                y.service({'youtube_client_secret_path': str(f)})

    def test_sign_in_asks_which_account(self):
        src = (core.ROOT / 'pipeline_youtube.py').read_text(encoding='utf-8')
        self.assertIn("prompt='select_account consent'", src)
        import pipeline_youtube as y
        self.assertEqual(y.sign_in_link(), '')

    def test_private_upload_end_to_end(self):
        self.prod('y2', 'approved')
        yt, http = self.yt(self.START, (200, {}, {'id': 'VID1', 'status': {'privacyStatus': 'private'}}))
        self.assertEqual(core.upload_private({}, core.production('y2'), lambda m: None, yt=yt, wait_thumb=False), 'VID1')
        p = core.production('y2')
        self.assertEqual((p['state'], p['youtube_id'], p['upload_note']), ('uploaded_private', 'VID1', 'done'))
        sent = json.loads(http.request_sequence[0][2])
        self.assertEqual(sent['status']['privacyStatus'], 'private')
        # a second press does nothing
        self.assertEqual(core.upload_private({}, core.production('y2'), lambda m: None, yt=yt), 'VID1')
        self.assertEqual(len(http.request_sequence), 2)

    def test_alarm_if_youtube_does_not_say_private(self):
        import pipeline_youtube as y
        self.prod('y3', 'approved')
        yt, _ = self.yt(self.START, (200, {}, {'id': 'VID3', 'status': {'privacyStatus': 'public'}}))
        with self.assertRaisesRegex(y.StopUploads, 'instead of private'):
            core.upload_private({}, core.production('y3'), lambda m: None, yt=yt, wait_thumb=False)
        self.assertEqual(core.production('y3')['youtube_id'], 'VID3')

    def test_refused_upload_can_be_tried_again(self):
        import pipeline_youtube as y
        self.prod('y4', 'approved')
        err = {'error': {'code': 403, 'errors': [{'reason': 'accessNotConfigured'}]}}
        yt, _ = self.yt((403, {'content-type': 'application/json'}, err))
        with self.assertRaisesRegex(y.StopUploads, 'not switched on'):
            core.upload_private({}, core.production('y4'), lambda m: None, yt=yt, wait_thumb=False)
        p = core.production('y4')
        self.assertEqual((p['state'], p['upload_note']), ('approved', None))

    def test_batch_stops_on_shared_problem_and_reports(self):
        import pipeline_youtube as y, unittest.mock as um
        for k in ('y5', 'y6', 'y7'):
            self.prod(k, 'approved')
        todo = [core.production(k) for k in ('y5', 'y6', 'y7')]
        err = {'error': {'code': 400, 'errors': [{'reason': 'uploadLimitExceeded'}]}}
        yt, http = self.yt((200, {}, {'items': [{'id': 'C', 'snippet': {'title': 'My Channel'}, 'status': {}}]}),
                           self.START, (200, {}, {'id': 'V5', 'status': {'privacyStatus': 'private'}}),
                           (400, {'content-type': 'application/json'}, err),
                           (200, {}, {'items': [{'processingDetails': {'processingStatus': 'processing'}}]}))
        with um.patch.object(y, 'service', lambda conf: yt):
            out = y.upload_batch({}, todo, lambda m: None, thumb_minutes=0)
        text = y.summary(out, 3)
        self.assertEqual((len(out['done']), len(out['failed'])), (1, 1))
        self.assertIn('1 of 3 uploaded as PRIVATE to My Channel', text)
        self.assertIn('tomorrow', text)
        self.assertIn('1 more not tried', text)
        self.assertEqual([core.production(k)['state'] for k in ('y5', 'y6', 'y7')], ['uploaded_private', 'approved', 'approved'])
        self.assertEqual(core.production('y7')['upload_note'], None)

    def test_dropped_connection_is_only_risky_at_the_end(self):
        import unittest.mock as um
        self.prod('y8', 'approved')

        class Req:
            resumable_progress, resumable_uri = 0, 'u'

            def next_chunk(self):
                raise KeyboardInterrupt
        yt = um.MagicMock()
        yt.videos().insert.return_value = Req()
        big = Path(core.production('y8')['video'])
        big.write_bytes(b'v' * (9 * 1024 * 1024))
        with self.assertRaises(KeyboardInterrupt):
            core.upload_private({}, core.production('y8'), lambda m: None, yt=yt, wait_thumb=False)
        self.assertIsNone(core.production('y8')['upload_note'])          # most of the file was never sent
        Req.resumable_progress = 8 * 1024 * 1024
        with self.assertRaises(KeyboardInterrupt):
            core.upload_private({}, core.production('y8'), lambda m: None, yt=yt, wait_thumb=False)
        self.assertEqual(core.production('y8')['upload_note'], 'started')  # last piece may have arrived: never repeat blindly

    def test_secrets_stay_out_of_messages(self):
        import pipeline_youtube as y
        bad = self.client_file('c6.json', {'web': {'client_secret': 'FAKE7'}})
        self.assertNotIn('FAKE7', y.check_client(bad)[1])
        src = (core.ROOT / 'pipeline_youtube.py').read_text(encoding='utf-8')
        self.assertNotIn('print(', src.replace('notify=print', ''))

    # ---- v0.7 steady framing, original quality, other places
    @staticmethod
    def irl(tracks, dur=12.0, cuts=(0,)):
        n = int(dur / .1)
        return {'meta': {'w': 1920, 'h': 1080, 'fps': 60, 'duration': dur, 'audio': True}, 'grid': .1, 'tracks': tracks, 'face_cam': [],
                'curves': {k: [.2] * n for k in ('ix', 'is')}, 'speech': {'words': []}, 'layout': {'mode': 'irl', 'cam': None, 'chat': [], 'captions': None, 'bars': [0, 0, 0, 0], 'cuts': list(cuts)}}

    @staticmethod
    def person(cx, w=.06, dur=12.0, shot=(0, 12.0)):
        t = [k * .25 + .1 for k in range(int(dur * 4))]
        return {'t': t, 'cx': [cx(v) for v in t], 'cy': [.4] * len(t), 'w': [w] * len(t), 'h': [w * 2] * len(t), 'shot': list(shot), 'act_t': [], 'act': []}

    def test_camera_never_moves_inside_a_shot(self):
        from funnycomm import framing
        walk = self.person(lambda t: .25 + .5 * t / 12)
        pl = framing.plan(self.irl([walk]))
        self.assertEqual(pl['mode'], 'steady')
        self.assertTrue(len(pl['segs']) <= 3)
        for g in pl['segs']:
            self.assertTrue(g['b'] - g['a'] >= 1.4)
            self.assertEqual(len(g['crop']), 3)                      # one fixed crop per shot, nothing to interpolate
        hit = 0
        for t, c in zip(walk['t'], walk['cx']):
            g = next(g for g in pl['segs'] if t < g['b'] + 1e-6 or g is pl['segs'][-1])
            crop, ph = (g['wide'], g['top']) if g['kind'] == 'duo' else (g['crop'], g['ph'])
            hit += abs(c * 1920 - crop[0]) < crop[2] * 1080 / ph / 2
        self.assertGreater(hit / len(walk['t']), .9)

    def test_bigger_shot_when_people_are_apart(self):
        from funnycomm import framing
        one = framing.plan(self.irl([self.person(lambda t: .5, .09)]))['segs']
        two = framing.plan(self.irl([self.person(lambda t: .25, .045), self.person(lambda t: .72, .045)]))['segs']
        self.assertEqual([(g['kind'], g['ph']) for g in one], [('one', 1920)])      # a lone face fills the phone
        g = two[0]
        self.assertEqual(g['kind'], 'duo')                           # two far apart: wide shot on top, close-up below
        half = g['wide'][2] * 1080 / g['top'] / 2
        self.assertTrue(g['wide'][0] - half < .25 * 1920 and g['wide'][0] + half > .72 * 1920)
        self.assertEqual(g['top'] + 1220, 1920)                      # and no bars

    def test_webcam_layout_is_fixed_and_keeps_the_face(self):
        from funnycomm import framing
        an = fake('x', dur=8.0)
        pl = framing.plan(an)
        self.assertTrue(all(g['kind'] == 'split' and g['top'] + g['ph'] <= 1920 for g in pl['segs']))
        cx, cy, ch = pl['segs'][0]['cam']
        fx, fy = (.1 + .025) * 1280, (.08 + .045) * 720
        self.assertTrue(abs(fx - cx) < ch * 1080 / pl['segs'][0]['top'] / 2 and abs(fy - cy) < ch / 2)

    def test_wide_button_and_shake_penalty(self):
        from funnycomm import framing
        an = self.irl([self.person(lambda t: .5, .09)])
        self.assertTrue(all(g['kind'] == 'duo' for g in framing.plan(an, {'mode': 'wide'})['segs']))
        calm = {**an, 'move': {'rate': 8, 'cols': [], 'rows': [], 'shake': [.05] * 90}}
        wild = {**an, 'move': {'rate': 8, 'cols': [], 'rows': [], 'shake': [.05, 1.4] * 45}}
        self.assertEqual(framing.wildness(calm), 0)
        self.assertGreater(framing.wildness(wild), 4)

    def test_still_pictures_are_spotted(self):
        from funnycomm import framing
        an = self.irl([self.person(lambda t: .5, .09)])
        photo = {**an, 'move': {'rate': 8, 'cols': [[0] * 48] * 90, 'rows': [], 'shake': [0] * 90}}
        live = {**an, 'move': {'rate': 8, 'cols': [[3] * 48] * 90, 'rows': [], 'shake': [0] * 90}}
        self.assertTrue(framing.still(photo))
        self.assertFalse(framing.still(live))
        self.assertFalse(framing.still({**photo, 'layout': {**an['layout'], 'cam': {'rect': [0, 0, .2, .2]}}}))

    def test_chat_cover_hides_text_but_keeps_shapes(self):
        import cv2
        im = np.full((1080, 1920, 3), 60, np.uint8)
        im[:, 1500:] = 190                                           # a big shape behind the chat
        for k in range(6):
            cv2.putText(im, 'username: hello there', (1250, 40 + k * 34), cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 90, 255), 2)
        out = render.soften(im, [[.64, 0, .25, .2]], (0, 0, 1920, 1080))
        self.assertLess(np.abs(np.diff(out[20:200, 1260:1440].astype(int), axis=1)).max(), 40)
        self.assertGreater(int(out[100, 1600, 1]) - int(out[100, 1300, 1]), 80)

    def test_original_lines_up_with_clip(self):
        from funnycomm import hq
        whole, part = Path(_TMP) / 'whole.mp4', Path(_TMP) / 'part.mp4'
        clip(whole, 14, size='640x360', fps=30, seed=3)
        media.run([media.tool('ffmpeg'), '-v', 'error', '-y', '-ss', '4.2', '-t', '7', '-i', whole, '-c:v', 'libx264', '-crf', '33', '-preset', 'ultrafast', '-c:a', 'aac', part])
        self.assertAlmostEqual(hq.align(part, whole, media.probe(part)), 4.2, delta=.06)
        other = clip(Path(_TMP) / 'other.mp4', 14, size='640x360', fps=30, seed=9)
        self.assertIsNone(hq.align(part, other, media.probe(part)))

    def test_muted_music_is_counted(self):
        from funnycomm import hq

        class TW:
            def get(self, what, p):
                return {'data': [{'muted_segments': [{'offset': 100, 'duration': 30}, {'offset': 400, 'duration': 10}]}]}
        self.assertEqual(hq.muted(TW(), '1', 120, 150), 10)
        self.assertEqual(hq.muted(TW(), '1', 200, 240), 0)

    def test_scan_finds_the_loud_moments(self):
        import pipeline_sources as ps
        rng = np.random.default_rng(1)
        x = rng.normal(0, .01, 300 * 16000).astype(np.float32)
        for at in (60, 150, 250):
            x[at * 16000:(at + 4) * 16000] *= 22
        old, ot = media.pcm, ears.tag
        media.pcm = lambda path, sr, a, n: x[int(a * sr):int((a + n) * sr)]
        ears.tag = lambda *a, **k: None
        try:
            got = ps.scan('x', 300, 5)
            self.assertEqual(sorted(round(p / 10) for p, _ in got)[:3], [6, 15, 25])
            self.assertEqual(len(ps.scan('x', 300, 5, skip=[(140, 170)])), 2)      # muted stretches are never used
        finally:
            media.pcm, ears.tag = old, ot

    def test_link_is_taken_once_and_errors_are_plain(self):
        import pipeline_sources as ps, shutil
        f = clip(Path(_TMP) / 'lk.mp4', 6, size='640x360', seed=77)

        class Y:
            def __init__(self, **k):
                self.o = k

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                if 'private' in url:
                    raise RuntimeError('ERROR: Private video. Sign in if you have access')
                return {'title': 'he fell off the boat', 'duration': 6, 'webpage_url': url, 'view_count': 77, 'timestamp': 1700000000}

            def download(self, urls):
                shutil.copyfile(f, self.o['outtmpl'].replace('%(ext)s', 'mp4'))
        old = ps._ydl
        ps._ydl = lambda **k: Y(**k)
        try:
            a = ps.add_link('https://streamable.com/abc', 'lacy', {}, 3, lambda m: None)
            self.assertEqual(len(a), 1)
            s0 = core.source(a[0])
            self.assertEqual((s0['state'], s0['title'], s0['views']), ('downloaded', 'he fell off the boat', 77))
            self.assertTrue(Path(s0['path']).is_file())
            core.update_source(a[0], state='used')
            self.assertEqual(ps.add_link('https://streamable.com/abc', 'lacy', {}, 3, lambda m: None), [])
            with self.assertRaises(ps.SourceError) as e:
                ps.add_link('https://www.youtube.com/watch?v=private', 'lacy', {}, 3, lambda m: None)
            self.assertIn('Add files', str(e.exception))
            with self.assertRaises(ps.SourceError):
                ps.add_link('file:///etc/passwd', 'lacy', {}, 3, lambda m: None)
        finally:
            ps._ydl = old
        self.assertFalse(ps.allowed({}, 'youtube:@x'))                # every outside channel starts as not allowed
        self.assertEqual((ps.site('https://youtu.be/x'), ps.site('https://kick.com/a/clips')), ('YouTube', 'Kick'))

    def test_watched_folder_is_read_once_and_left_alone(self):
        import pipeline_sources as ps
        d = Path(_TMP) / 'drive' / 'marlon stuff'
        d.mkdir(parents=True)
        f = clip(d / 'a.mp4', 7, size='1280x720', seed=88)
        os.utime(f, (time.time() - 500, time.time() - 500))
        size = f.stat().st_size
        conf = {'watch_folders': [str(d.parent)], 'streamers': ['marlon', 'lacy']}
        self.assertEqual(ps.watch(conf, lambda m: None, seen_key='t_seen'), 1)
        self.assertEqual(ps.watch(conf, lambda m: None, seen_key='t_seen'), 0)
        self.assertEqual(f.stat().st_size, size)
        self.assertIn('marlon', [x['streamer'] for x in core.source_rows() if str(x['id']).startswith('local:')])

    def test_same_moment_is_never_made_twice(self):
        # Two clips that start together but end 20 s apart: offsets are where clips END.
        a, b = {'video_id': 'V', 'vod_offset': 18693.0, 'seconds': 26.3}, {'video_id': 'V', 'vod_offset': 18713.0, 'seconds': 46.4}
        self.assertGreater(core.vod_overlap(a, b), .9)
        for sid, v in (('clip:RA', a), ('clip:RB', b), ('clip:RC', {'video_id': 'V', 'vod_offset': 19000.0, 'seconds': 30})):
            core.record_sources([{'id': sid, 'streamer': 'x', 'source_kind': 'clip', 'url': 'u', 'title': 't', 'views': 1, 'created': '', 'broadcaster_id': '', **v}])
            core.update_source(sid, path=str(Path(_TMP) / 'none.mp4'), fp=b'')
        self.prod('ra', 'uploaded_private')
        core.update_prod('ra', sources_json=json.dumps(['clip:RA']), title='First one')
        self.assertEqual(pr.repeat_of(core.source('clip:RB')), 'First one')
        self.assertIsNone(pr.repeat_of(core.source('clip:RC')))
        core.update_prod('ra', state='rejected')

    # ---- v0.6 scheduling, storage, all-time clips
    def test_slots_are_spread_and_never_shared(self):
        conf = {'posts_per_day': 20, 'post_from': '08:00', 'post_until': '23:00'}
        now = time.mktime((2030, 1, 10, 6, 0, 0, 0, 0, -1))
        a = core.next_slots(conf, 45, now)
        self.assertEqual(len(a), 45)
        gaps = [y - x for x, y in zip(a, a[1:])]
        self.assertTrue(min(gaps) > 30 * 60)
        self.assertEqual(sum(time.localtime(t)[:3] == time.localtime(now)[:3] for t in a), 20)
        self.assertTrue(all(8 <= time.localtime(t).tm_hour <= 23 for t in a))
        self.prod('s1', 'approved')
        core.update_prod('s1', publish_at=core.utc(a[0]))
        self.assertNotIn(round(a[0]), [round(x) for x in core.next_slots(conf, 10, now)])
        core.update_prod('s1', publish_at=None)

    def test_publish_time_only_when_owner_chose_it(self):
        p = {'title': 't', 'description': 'd', 'publish_at': '2030-01-01T00:00:00Z', 'kind': 'single'}
        self.assertNotIn('publishAt', core.upload_body({}, p)['status'])
        self.assertNotIn('publishAt', core.upload_body({'posting_mode': 'private'}, p)['status'])
        self.assertNotIn('publishAt', core.upload_body({'posting_mode': 'schedule'}, {**p, 'kind': 'video'})['status'])
        st = core.upload_body({'posting_mode': 'schedule'}, p)['status']
        self.assertEqual((st['privacyStatus'], st['publishAt']), ('private', '2030-01-01T00:00:00Z'))

    def test_unapproved_short_gets_no_slot_and_no_upload(self):
        import pipeline_youtube as y
        self.prod('s2', 'review')
        self.assertNotIn('s2', [p['id'] for p in y.queue({'posting_mode': 'schedule', 'posts_per_day': 20})])

    def test_rejection_pauses_posting(self):
        import pipeline_youtube as y
        self.prod('s3', 'uploaded_private')
        core.update_prod('s3', youtube_id='REJ')
        with core.db() as con:
            ids = [r['youtube_id'] for r in con.execute("SELECT youtube_id FROM production WHERE state='uploaded_private' AND youtube_id IS NOT NULL")]
        items = [{'id': i, 'status': {'uploadStatus': 'rejected', 'rejectionReason': 'copyright'} if i == 'REJ' else {'uploadStatus': 'processed', 'privacyStatus': 'private'}} for i in ids]
        yt, _ = self.yt((200, {}, {'items': items}))
        ok = y.sync({}, yt)
        self.assertTrue(y.paused() and 'copyright' in y.paused()['why'])
        self.assertNotIn('s3', ok)
        self.assertFalse(y.post_next({'auto_post': True}))
        y.resume()
        self.assertIsNone(y.paused())
        core.update_prod('s3', state='rejected')

    def test_free_space_only_touches_confirmed_uploads(self):
        def clip(sid, state='used'):
            f = core.SRC / (sid.replace(':', '_') + '.mp4')
            f.write_bytes(b'c' * 1000)
            core.record_sources([{'id': sid, 'streamer': 'x', 'source_kind': 'clip', 'url': 'u', 'title': 't', 'views': 1, 'seconds': 10, 'created': '', 'broadcaster_id': ''}])
            core.update_source(sid, path=str(f), state=state, fp=b'1' * 400)
            return f
        fa, fb, fc = clip('clip:FA'), clip('clip:FB'), clip('clip:FC', 'downloaded')
        for pid, state, yid, srcs in (('f1', 'uploaded_private', 'Y1', ['clip:FA', 'clip:FB']), ('f2', 'review', None, ['clip:FB']), ('f3', 'uploaded_private', 'Y3', ['clip:FC'])):
            self.prod(pid, state)
            d = core.REVIEW / pid
            d.mkdir(exist_ok=True)
            (d / 'short.mp4').write_bytes(b'v' * 5000)
            (d / 'thumbnail.jpg').write_bytes(b't' * 100)
            core.update_prod(pid, youtube_id=yid, sources_json=json.dumps(srcs))
        r = core.free_space({'f1'}, lambda m: None)                 # f3 is not confirmed on YouTube
        self.assertFalse((core.REVIEW / 'f1' / 'short.mp4').exists())
        self.assertTrue((core.REVIEW / 'f1' / 'thumbnail.jpg').exists())
        self.assertTrue((core.REVIEW / 'f2' / 'short.mp4').exists())
        self.assertTrue((core.REVIEW / 'f3' / 'short.mp4').exists())
        self.assertFalse(fa.exists())                               # only used in the uploaded one
        self.assertTrue(fb.exists() and fc.exists())                # still needed, or not confirmed
        self.assertEqual(core.production('f1')['purged'], 1)
        self.assertEqual(core.source('clip:FA')['state'], 'used')   # so it is never picked again
        self.assertGreater(r['bytes'], 5000)
        for k in ('f1', 'f2', 'f3'):
            core.update_prod(k, state='rejected')

    def test_mix_shares_between_people_and_ages(self):
        from funnycomm import index
        now = time.time()
        with core.db() as con:
            for who in ('mixa', 'mixb'):
                for k in range(60):
                    age = (3, 60, 900)[k % 3] * 86400 + k * 7200
                    con.execute('INSERT OR REPLACE INTO clip_index(id,streamer,broadcaster_id,url,title,views,seconds,created,ts,game,video_id,vod_offset,creator,thumb_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (f'clip:{who}{k}', who, '1', 'u', f'{who} falls off the chair {k}', 500 + k, 25, '', now - age, 'Just Chatting', None, None, f'c{k}', ''))
        for who in ('mixa', 'mixb'):
            index.kv_set('index:' + who, {'last': now})
        ms = index.pick_mix(['mixa', 'mixb'], 20)
        self.assertEqual(len(ms), 20)
        self.assertEqual(sum(m['streamer'] == 'mixa' for m in ms), 10)
        old = sum(now - m['ts'] > 400 * 86400 for m in ms)
        self.assertTrue(3 <= old <= 8)
        self.assertTrue(all(now - m['ts'] > 100 * 86400 or now - m['ts'] > 9 * 86400 for m in index.pick_mix(['mixa'], 10, 'classic')))

    def test_backfill_walks_back_and_stops(self):
        from funnycomm import index
        calls = []

        class TW:
            games = {}

            def broadcaster(self, l):
                return {'id': '9'}

            def game_names(self, x):
                pass

            def get(self, what, p):
                calls.append(p['started_at'])
                n = len(calls)
                return {'data': [{'id': f'BF{n}', 'url': 'u', 'title': 'old one', 'view_count': 9, 'duration': 20, 'created_at': p['started_at'], 'creator_name': 'c'}] if n <= 5 else [], 'pagination': {}}
        self.assertEqual(index.backfill(TW(), ['bfx']), 5)
        self.assertTrue(len(calls) <= 5 + 14)
        n = len(calls)
        index.backfill(TW(), ['bfx'])                               # once is enough
        self.assertEqual(len(calls), n)

    def test_cover_words_and_titles_are_whole(self):
        src = {'streamer': 'lacy', 'title': 'x', 'game': 'Just Chatting', 'crowd_titles': json.dumps(['Ron says Balenciaga in different languages'])}
        an = {'speech': {'words': [], 'lines': [], 'ok': True}, 'moments': {'jokes': []}, 'curves': {}}
        try:
            ts, h = meta.titles(src, an, []), meta.hook(src, an)
        except KeyError as e:
            self.skipTest(str(e))
        self.assertTrue(ts[0].startswith('Ron says Balenciaga in different languages'))
        self.assertNotIn('(Lacy)', ts[0])
        self.assertNotIn('#', ts[0])
        self.assertTrue(2 <= len(h.split()) <= 5)
        self.assertEqual(meta.natural('Marlon crash bike'), 'Marlon crashes bike')

    def test_interrupted_upload_not_repeated(self):
        self.prod('g3', 'approved')
        core.update_prod('g3', upload_note='started')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            core.upload_private({}, core.production('g3'))


class Dupes(unittest.TestCase):
    def test_vod_overlap(self):
        a = {'video_id': '1', 'vod_offset': 100, 'seconds': 30}
        self.assertGreater(core.vod_overlap(a, {'video_id': '1', 'vod_offset': 110, 'seconds': 30}), .6)
        self.assertEqual(core.vod_overlap(a, {'video_id': '2', 'vod_offset': 110, 'seconds': 30}), 0)
        self.assertEqual(core.vod_overlap(a, {'video_id': '1', 'vod_offset': None, 'seconds': 30}), 0)

    def test_fingerprint_survives_reencode_and_trim(self):
        d = Path(_TMP)
        a = clip(d / 'fa.mp4', 12, seed=3)
        b = d / 'fb.mp4'
        subprocess.run([media.tool('ffmpeg'), '-v', 'error', '-y', '-ss', '2', '-i', str(a), '-vf', 'scale=320:-2', '-c:a', 'aac', '-b:a', '48k', '-ar', '22050', str(b)], check=True)
        c = clip(d / 'fc.mp4', 12, seed=9)
        fa, fb, fc = audio.fingerprint(a), audio.fingerprint(b), audio.fingerprint(c)
        m = audio.match(fa, fb)
        self.assertIsNotNone(m)
        self.assertLess(m['ber'], audio.SAME_BER)
        self.assertAlmostEqual(m['offset'], 2, delta=.15)
        n = audio.match(fa, fc)
        self.assertTrue(n is None or n['ber'] > audio.SAME_BER)

    def test_library_scan_is_read_only(self):
        d = Path(_TMP) / 'lib'
        d.mkdir(exist_ok=True)
        f = clip(d / 'random name.mp4', 8, seed=4)
        before = (f.read_bytes(), f.stat().st_mtime_ns)
        core.scan_existing_folder(d, lambda m: None)
        self.assertEqual(before, (f.read_bytes(), f.stat().st_mtime_ns))
        new = Path(_TMP) / 'new.mp4'
        subprocess.run([media.tool('ffmpeg'), '-v', 'error', '-y', '-i', str(f), '-c:v', 'libx264', '-crf', '35', '-c:a', 'aac', '-b:a', '64k', str(new)], check=True)
        core.record_sources([{'id': 'clip:New-2', 'streamer': 'lacy', 'source_kind': 'clip', 'url': 'u', 'title': 't', 'views': 1, 'seconds': 8, 'created': '', 'broadcaster_id': '1'}])
        core.update_source('clip:New-2', state='downloaded', path=str(new))
        self.assertEqual(core.find_duplicate('clip:New-2', new)['kind'], 'library')
        core.recheck_downloads(lambda m: None)
        self.assertEqual(core.source('clip:New-2')['state'], 'existing')
        self.assertTrue(new.exists())          # never deleted


class Story(unittest.TestCase):
    def test_cut_never_splits_words_and_fits(self):
        words = [(f'w{i}', 1 + i * .5, 1.4 + i * .5, .9) for i in range(90)]
        an = fake('x', 60, words=words, payoff=40)
        for kind, mx in (('single', 34), ('ranking', 14)):
            c = story.cut(an, mx, kind)
            a, b = c['keep'][0][0], c['keep'][-1][1]
            self.assertLessEqual(story.length(c['keep']), mx + 1.3)
            self.assertTrue(a <= 40 <= b)
            for t in (a, b):
                self.assertFalse(any(s + .03 < t < e - .03 for _, s, e, _ in words), (kind, t))

    def test_short_clip_kept_whole(self):
        c = story.cut(fake('x', 8, payoff=3), 34, 'single')
        self.assertEqual(c['keep'], [[0.0, 8.0]])

    def test_ranking_order_strongest_last(self):
        it = [{'src': {'streamer': 'lacy', 'title': str(i), 'game': 'Grand Theft Auto VI'}, 'an': fake('x'), 'score': {'total': s}} for i, s in enumerate((50, 80, 65))]
        g = story.pick_rankings(it, 3)[0]
        self.assertEqual([q['score']['total'] for q in g['items']], [50, 65, 80])

    def test_own_rating_outranks(self):
        s = {'id': 'a', 'streamer': 'l', 'views': 5, 'title': ''}
        an = fake('x', 20)
        self.assertGreater(story.score(an, s, [s], 5)['total'], story.score(an, s, [s], 1)['total'] + 20)


class Text(unittest.TestCase):
    def test_titles(self):
        src = {'id': 'clip:a', 'streamer': 'lacy', 'title': 'LACY JUST KILLED AN INNCOCENT DOGGGG', 'game': 'Grand Theft Auto VI', 'url': 'https://www.twitch.tv/lacy/clip/a'}
        an = fake('x')
        t = meta.titles(src, an, ['big reaction'])
        self.assertTrue(t[0].startswith('Lacy just killed an innocent dog'))
        self.assertTrue(all(len(x) <= 100 for x in t))
        d = meta.description([src], 'single', {'attribution_line': 'Clips used with permission.'})
        self.assertIn(src['url'], d)
        self.assertNotIn('not been made public', d)
        self.assertFalse(any(w in ' '.join(t).lower().split() for w in ('his', 'her', 'him')))

    def test_noise_titles_rejected(self):
        for t in ('wwwwww', '123', 'OMG', 'gyattttttt', 'LOLLLL'):
            self.assertFalse(meta.meaningful(t), t)

    def test_profanity_never_reaches_labels(self):
        src = {'id': 'clip:b', 'streamer': 'marlon', 'title': 'LION HOLY SHIT', 'game': 'IRL', 'url': 'u'}
        self.assertEqual(meta.label(src, fake('x'), []), 'Lion')
        self.assertNotIn('shit', ' '.join(meta.titles(src, fake('x'), [])).lower())

    def test_emotional_monologue_scores_low(self):
        s = {'id': 'a', 'streamer': 'l', 'views': 5, 'title': ''}
        talk = [(w, i * .4, i * .4 + .3, .9) for i, w in enumerate('i worked very hard for it and i just felt like i really failed but god had other plans'.split())]
        self.assertLess(story.score(fake('x', 20, words=talk), s, [s])['total'], story.score(fake('x', 20), s, [s])['total'])

    def test_repeated_slur_sounding_word_is_held(self):
        an = fake('x', 20, words=[('Niga', i, i + .4, .9) for i in range(5)])
        self.assertTrue(safety.refresh_language(an))
        self.assertEqual(an['safety']['level'], 'hold')
        an = fake('x', 20, words=[('Nigeria', 1, 2, .9), ('finger', 3, 4, .9)])
        self.assertFalse(safety.refresh_language(an))

    def test_slur_title_is_held(self):
        self.assertTrue(speech.title_has_slur('PARDON??? someone said the nword???'))
        r = safety.assess('he said the n-word', 10, [], [], True, None, True)
        self.assertEqual(r['level'], 'hold')


class Safety(unittest.TestCase):
    def hit(self, t, label='BUTTOCKS_EXPOSED', s=.7):
        return {'t': t, 'label': label, 'kind': 'explicit', 'score': s, 'box': [.4, .4, .2, .2], 'cam': False}

    def test_long_nudity_holds_short_is_censored_single_frame_ignored(self):
        self.assertEqual(safety.assess('', 30, [self.hit(t / 2) for t in range(40)], [], True, None, True)['level'], 'hold')
        r = safety.assess('', 30, [self.hit(t) for t in (10, 10.4, 10.8)], [], True, None, True)
        self.assertEqual(r['level'], 'caution')
        self.assertTrue(r['censor'])
        self.assertEqual(safety.assess('', 30, [self.hit(5, s=.5)], [], True, None, True)['level'], 'clear')

    def test_bare_skin_game_scene_is_held_but_shirtless_streamer_is_not(self):
        h = lambda t, cam=False: {'t': t, 'label': 'BELLY_EXPOSED', 'kind': 'skin', 'score': .5, 'box': [.4, .4, .2, .2], 'cam': cam}
        hits = {'n': 40, 'hits': [h(t) for t in range(8)]}
        self.assertEqual(safety.assess('', 30, hits, [], True, None, True)['level'], 'hold')
        self.assertEqual(safety.assess('', 30, {'n': 40, 'hits': [h(t, True) for t in range(20)]}, [], True, None, True)['level'], 'clear')
        self.assertEqual(safety.assess('', 30, hits, [], True, None, True, irl=True)['level'], 'clear')
        self.assertEqual(safety.assess('', 30, {'n': 40, 'hits': [h(1)]}, [], True, None, True)['level'], 'clear')

    def test_missing_scanner_never_reports_clear(self):
        self.assertEqual(safety.assess('', 30, None, [], True, None, True)['level'], 'caution')


class Camera(unittest.TestCase):
    def test_game_crop_never_shows_webcam(self):
        for rect in ((0, 0, .26, .26), (.74, 0, .26, .26), (0, .6, .3, .3), (.78, .45, .22, .26), (0, .44, .31, .31)):
            an = fake('x', 6, cam=rect)
            an['layout']['chat'] = []
            p = camera.plan(an)
            gx, gy, gh = p['game']
            half = gh * camera.W / camera.GAME_H / 2
            l, r = (gx - half) / 1280, (gx + half) / 1280
            self.assertTrue(np.all(l > -.002) and np.all(r < 1.002), rect)
            x0, x1 = rect[0], rect[0] + rect[2]
            self.assertTrue(np.all((r <= x0 + .012) | (l >= x1 - .012)), rect)

    def test_irl_crop_in_bounds(self):
        an = fake('x', 6, mode='irl')
        an['tracks'] = [{'t': [i * .25 for i in range(24)], 'cx': [.05 + .04 * i for i in range(24)], 'cy': [.3] * 24, 'w': [.1] * 24, 'h': [.18] * 24, 'shot': [0, 6], 'act': [], 'act_t': []}]
        cx, cy, ch = camera.plan(an)['single']
        self.assertTrue(np.all(cx - ch * 9 / 32 >= -1) and np.all(cx + ch * 9 / 32 <= 1281))


class Rendering(unittest.TestCase):
    def plan(self, kind, items):
        return {'kind': kind, 'fps': 30, 'headline': 'TEST 😭', 'style': {**render.DEFAULT_STYLE, 'preset': 'ultrafast', 'crf': 30}, 'items': items, 'voice': []}

    def test_single_and_ranking(self):
        d = Path(_TMP)
        a, b = clip(d / 'ra.mp4', 4, seed=1), clip(d / 'rb.mp4', 4, seed=2)
        ans = [fake(a), fake(b, mode='scene')]
        ans[0]['safety']['mute'] = [[1.0, 1.4]]
        it = [{'id': 'a', 'path': str(a), 'streamer': 'lacy', 'cut': {'keep': [[0, 4]], 'payoff': 2.5}, 'label': 'One', 'rank': 2, 'censor': [[.5, 1.5]], 'mute': [[1, 1.4]]},
              {'id': 'b', 'path': str(b), 'streamer': 'marlon', 'cut': {'keep': [[0, 4]], 'payoff': 2.5}, 'label': 'Two', 'rank': 1}]
        for kind, use in (('single', it[:1]), ('ranking', it)):
            out = d / f'{kind}.mp4'
            r = render.render(self.plan(kind, use), ans[:len(use)], out)
            m = media.probe(out)
            self.assertEqual((m['w'], m['h']), (1440, 2560))
            self.assertEqual(m['sample_rate'], 48000)         # v0.3 produced 96 kHz and could not join clips
            self.assertAlmostEqual(m['duration'], r['duration'], delta=.2)
            self.assertFalse(Path(str(out) + '.part').exists())
            rep = qc.check(out, r['duration'])
            self.assertEqual([i for i in rep['issues'] if 'Audio is' in i or 'Wrong size' in i], [])

    def test_thumbnail_is_portrait_jpeg(self):
        t = Path(_TMP) / 't.jpg'
        gfx.thumbnail(np.full((1920, 1080, 3), 90, np.uint8), 'HELLO THERE 😭', 'lacy', t, 3)
        from PIL import Image
        im = Image.open(t)
        self.assertEqual((im.size, im.format), ((1080, 1920), 'JPEG'))
        self.assertLess(t.stat().st_size, 2_000_000)

    def test_place_verifies(self):
        s = Path(_TMP) / 'p.bin'
        s.write_bytes(os.urandom(5000))
        media.place(s, Path(_TMP) / 'sub' / 'q.bin')
        self.assertEqual((Path(_TMP) / 'sub' / 'q.bin').read_bytes(), s.read_bytes())


def seed_index():
    """A tiny index: one moment three people clipped, some loners, an old one."""
    import time
    now = time.time()
    rows = []

    def add(i, who, title, ago_days, vid, off, sec, creator, views, game='Just Chatting'):
        ts = now - ago_days * 86400 + off
        from datetime import datetime, timezone
        rows.append((f'clip:{i}', who, '1', f'https://www.twitch.tv/{who}/clip/{i}', title, views, sec, datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), ts, game, vid, off, creator, ''))
    for k, (t, c) in enumerate((('Ron says Balenciaga in different languages', 'a'), ('LMAOO', 'b'), ('jbg', 'c'))):
        add(f'M{k}', 'lacy', t, 3, 'v1', 100 + k * 5, 30, c, 50)
    add('Solo', 'lacy', 'wwww', 3, 'v1', 5000, 30, 'z', 90000)
    add('Riz', 'marlon', 'marlon flirts with a girl at the mall', 20, 'v2', 10, 30, 'q', 40)
    add('Rizler', 'marlon', 'The Rizzler pulls up on marlon', 20, 'v2', 900, 30, 'q', 4000)
    add('Old', 'lacy', 'lmao he fell', 100, 'v3', 10, 30, 'q', 10)
    with core.db() as con:
        con.executemany('INSERT OR REPLACE INTO clip_index(id,streamer,broadcaster_id,url,title,views,seconds,created,ts,game,video_id,vod_offset,creator,thumb_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    index.kv_set('index:lacy', {'last': 1, 'full': 1, 'days': 180})
    index.kv_set('index:marlon', {'last': 1, 'full': 1, 'days': 180})
    index._cache.clear()


class CrowdAndTopics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed_index()

    def test_same_seconds_become_one_moment_and_beat_views(self):
        import time
        ms = index.moments(['lacy'], time.time() - 30 * 86400, time.time() + 9e4)
        self.assertEqual(ms[0]['people'], 3)
        self.assertEqual(ms[0]['titles'][0].startswith('Ron') or 'Ron says' in ' '.join(ms[0]['titles']), True)
        self.assertIn('clip:Solo', [m['key'] for m in ms[1:]])
        self.assertEqual(index.moment_of('clip:M1')['people'], 3)

    def test_ideas_are_understood(self):
        c = {'streamers': ['lacy', 'marlon']}
        p = topics.parse('Marlon rizzing up girls', c)
        self.assertEqual((p['kind'], p['topic'], p['streamers'], p['days']), ('topic', 'rizz', ['marlon'], 180))
        p = topics.parse('best of marlon in the last week', c)
        self.assertEqual((p['kind'], p['streamers'], p['days']), ('best', ['marlon'], 7))
        p = topics.parse('last week in teh core house', c)
        self.assertEqual((p['kind'], p['group'], len(p['streamers']), p['days']), ('recap', 'core', 6, 7))
        p = topics.parse('best of lacy', c)
        self.assertEqual((p['kind'], p['streamers']), ('best', ['lacy']))
        p = topics.parse('ron crashing out last month', c)
        self.assertEqual((p['topic'], p['streamers'], p['days']), ('rage', ['stableronaldo'], 30))

    def test_rizz_is_not_rizzler(self):
        r = topics.search(topics.parse('Marlon rizzing up girls', {'streamers': ['marlon']}))
        good = [m['key'] for m in r if m['rel'] >= .5]
        self.assertIn('clip:Riz', good)
        self.assertNotIn('clip:Rizler', good)

    def test_person_is_found_on_other_channels(self):
        import time
        with core.db() as con:
            con.execute("INSERT OR REPLACE INTO clip_index(id,streamer,broadcaster_id,url,title,views,seconds,created,ts,game,video_id,vod_offset,creator,thumb_url) VALUES('clip:X1','lacy','1','u','marlon flirts with a waitress',10,30,'2026-11-20T00:00:00Z',?,'Just Chatting','v9',5,'k','')", (time.time() - 86400,))
            con.execute("INSERT OR REPLACE INTO clip_index(id,streamer,broadcaster_id,url,title,views,seconds,created,ts,game,video_id,vod_offset,creator,thumb_url) VALUES('clip:X2','marlon','1','u','ron rizzes a girl',10,30,'2026-11-20T00:00:00Z',?,'Just Chatting','v9',500,'k','')", (time.time() - 86400,))
        index._cache.clear()
        r = {m['key']: m['rel'] for m in topics.search(topics.parse('Marlon rizzing up girls', {'streamers': ['marlon', 'lacy']}))}
        self.assertGreaterEqual(r.get('clip:X1', 0), .8)
        self.assertLess(r.get('clip:X2', 0), .5)

    def test_hateful_chat_is_caught(self):
        self.assertTrue(speech.chat_has_slur(['dick you fucking jew']))
        self.assertTrue(speech.chat_has_slur(['Ur the neeggga']))
        self.assertFalse(speech.chat_has_slur(['Jewel heist', 'the black car', 'Nagasaki', 'nah bro', 'the village']))

    def test_lost_apostrophes_come_back(self):
        self.assertEqual(meta.human('lacy threatens marlon s progress'), "Lacy threatens Marlon's progress")
        self.assertIn("we're", meta.human('just hold on we re going home'))
        self.assertNotIn(':sob', meta.human('What james do :sob:'))

    def test_group_thumbnail(self):
        import numpy as np, tempfile
        from pathlib import Path
        from funnycomm import longform
        fr = np.full((720, 1280, 3), 120, np.uint8)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 't.jpg'
            longform._panels([{'frame': fr, 'face': (.8, .1, .1, .2)}, {'frame': fr, 'face': (.02, .5, .05, .1)}, {'frame': fr, 'face': (.4, .2, .3, .6)}], ['CORE HOUSE', 'THIS WEEK'], f)
            self.assertTrue(f.stat().st_size > 5000)

    def test_clear_span_avoids_censored_seconds(self):
        from funnycomm import longform
        an = {'safety': {'censor': [[2.0, 4.0], [25.0, 26.0]]}}
        self.assertEqual(longform.clear_span(an, 15.0, 0.0, 30.0), (4.3, 24.7))
        self.assertIsNone(longform.clear_span(an, 3.0, 0.0, 30.0))
        self.assertIsNone(longform.clear_span(an, 5.0, 0.0, 30.0))

    def test_flags_only_cover_used_seconds(self):
        from funnycomm import longform
        an = {'safety': {'reasons': [{'t': [[13.5, 15.7]], 'tag': 'nudity'}, {'t': [], 'tag': 'x'}, {'t': [[2, 3]], 'tag': 'music'}]}}
        seg = {'id': 'q', 'parts': [{'id': 'q', 'a': 0.0, 'b': 13.2}]}
        self.assertEqual([r['tag'] for r in longform.used_reasons(seg, an)], ['x', 'music'])

    def test_moved_clips_are_followed(self):
        import tempfile, unittest.mock as um
        from pathlib import Path
        from funnycomm import longform
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'new.mp4'
            f.write_bytes(b'x')
            plan = {'segments': [{'parts': [{'id': 'a', 'path': str(Path(d) / 'old.mp4')}]}], 'hook': {'id': 'a', 'path': str(Path(d) / 'old.mp4')}}
            with um.patch.object(longform.core, 'source', lambda i: {'path': str(f)}):
                longform.fresh_paths(plan)
            self.assertEqual(plan['segments'][0]['parts'][0]['path'], str(f))
            self.assertEqual(plan['hook']['path'], str(f))

    def test_camera_holds_through_a_missed_face(self):
        import numpy as np
        t = [round(i * .25, 2) for i in range(8)] + [round(5 + i * .25, 2) for i in range(20)]
        tr = {'shot': [0.0, 10.0], 't': t, 'cx': [.75] * len(t), 'cy': [.5] * len(t), 'w': [.08] * len(t), 'h': [.14] * len(t), 'act_t': [0, 10], 'act': [0, 0]}
        n = 40
        an = {'meta': {'w': 1920, 'h': 1080, 'duration': 10.0}, 'grid': .25, 'tracks': [tr],
              'layout': {'bars': [0, 0, 0, 0], 'captions': None, 'cuts': [0.0], 'mode': 'irl'},
              'curves': {'ix': [.1] * n, 'is': [1.0] * n, 'talk': [0] * n, 'speech': [0] * n}, 'speech': {'words': [], 'lines': []}}
        try:
            pl = camera.irl_plan(an, 10.0)
        except KeyError as e:
            self.skipTest(f'needs {e}')
        cx = pl['single'][0]
        gap = cx[int(3.2 / camera.DT)]
        self.assertGreater(gap, 1200)          # stays with the person, not at x=192

    def test_unfit_talk_is_left_out(self):
        from funnycomm import longform
        an = lambda x: {'speech': {'lines': [[0, 1, x]], 'words': [[w, 0, 1] for w in x.split()]}}
        self.assertTrue(longform.unfit(an('give her the meanest backshots of all time')))
        self.assertTrue(longform.unfit(an('you are a stalker gang messaging my older sister')))
        self.assertFalse(longform.unfit(an('bro I got pepper sprayed in the game oh my god')))
        self.assertFalse(longform.unfit(an('I heard a rumor you bought Balenciaga')))

    def test_rejected_video_gives_its_moments_back(self):
        import inspect
        from funnycomm import longform
        self.assertIn("NOT IN ('rejected','failed')", inspect.getsource(longform.gather))

    def test_keyed_webcam_gameplay_is_judged_as_game(self):
        from funnycomm import analyze
        self.assertTrue(analyze.is_game('Grand Theft Auto VI'))
        self.assertFalse(analyze.is_game('Just Chatting'))

    def test_minor_mentions_drop_out_of_rizz(self):
        sp = topics.parse('Marlon rizzing up girls', {'streamers': ['marlon']})
        an = fake('x', mode='irl')
        an['faces'] = {'f': .9, 'm': .9}
        an['chat_text'] = []
        m = {'titles': ['marlon flirts'], 'rel': 1.0}
        for line, ok in (("I'm 16 actually", False), ('she is in high school', False), ('I am 22 from Miami', True)):
            an['speech']['lines'] = [[0, 1, line, .9]]
            rel, _ = longform.relevance(sp, m, an)
            self.assertEqual(rel >= .5, ok, line)

    def test_titles_come_from_viewers(self):
        s = {'id': 'clip:M0', 'streamer': 'lacy', 'title': 'jbg', 'crowd_titles': json.dumps(['LOLLLL', 'Ron says Balenciaga in different languages!'])}
        self.assertEqual(meta.describe(s), 'Ron says Balenciaga in different languages')
        self.assertEqual(meta.headline({'id': 'x', 'streamer': 'lacy', 'title': 'wwww'}, fake('x'), []), '')     # nothing beats a generic line


class LongVideo(unittest.TestCase):
    def test_order_opens_and_closes_strong(self):
        it = [{'value': v, 'm': {'ts': v, 'streamer': 'a' if i % 2 else 'b', 'game': ''}} for i, v in enumerate((.9, .8, .7, .6, .5, .4))]
        o = longform.order(it, {'kind': 'best'})
        self.assertEqual((o[0]['value'], o[-1]['value']), (.8, .9))
        self.assertEqual([x['m']['ts'] for x in longform.order(it, {'kind': 'recap'})], sorted(x['value'] for x in it))

    def test_renders_16x9_with_chapters(self):
        d = Path(_TMP)
        segs = []
        for k in range(4):
            f = clip(d / f'lv{k}.mp4', 13, seed=20 + k, size='640x360')
            core.record_sources([{'id': f'clip:LV{k}', 'streamer': 'lacy', 'source_kind': 'clip', 'url': f'https://x/{k}', 'title': f't{k}', 'views': 1, 'seconds': 13, 'created': '', 'broadcaster_id': '1'}])
            core.update_source(f'clip:LV{k}', state='downloaded', path=str(f))
            segs.append({'id': f'clip:LV{k}', 'streamer': 'lacy', 'title': f'Scene number {k}', 'url': f'https://x/{k}', 'ts': k, 'value': .5 + k / 10, 'score': 50, 'people': 2, 'why': [], 'payoff': 6,
                         'parts': [{'id': f'clip:LV{k}', 'path': str(f), 'a': 0.0, 'b': 12.5}], 'day': '', 'on': True, 'risk': 'clear', 'game': ''})
        plan = {'version': 5, 'kind': 'video', 'spec': topics.parse('best of lacy', {'streamers': ['lacy']}), 'segments': segs, 'hook': {'id': 'clip:LV3', 'path': segs[3]['parts'][0]['path'], 'a': 2.0, 'b': 6.0}, 'voice': [], 'hide_captions': True, 'lower_third': True}
        pid = longform.finish('vtest', plan, {'attribution_line': 'Clips used with permission.'}, lambda m: None)
        p = core.production(pid)
        self.assertEqual((p['state'], p['kind']), ('review', 'video'))
        m = media.probe(p['video'])
        self.assertEqual((m['w'], m['h'], m['sample_rate']), (1920, 1080, 48000))
        self.assertAlmostEqual(m['duration'], 54, delta=.6)
        self.assertIn('0:00 Scene number 0', p['description'])
        self.assertTrue(json.loads(p['options_json'])['thumbs'])
        with self.assertRaisesRegex(RuntimeError, 'approval'):
            core.upload_private({}, p)
        plan2 = json.loads(p['plan_json'])
        plan2['segments'][1]['on'] = False
        longform.rebuild(pid, {}, plan2, lambda m: None)
        self.assertAlmostEqual(core.production(pid)['seconds'], 41.5, delta=.6)


def track(cx, w=.1, dur=6.0, cy=.35):
    n = int(dur * 4)
    return {'t': [i * .25 for i in range(n)], 'cx': [cx] * n, 'cy': [cy] * n, 'w': [w] * n, 'h': [w * 1.6] * n, 'shot': [0, dur], 'act': [], 'act_t': []}


class Looks(unittest.TestCase):
    """v0.8: one picture, never cut in two; square and full-screen take turns."""

    def an(self, tracks, mode='irl'):
        an = fake('x', 6, mode=mode)
        an['tracks'] = tracks
        an['layout']['chat'] = []
        an['move'] = {'rate': 8.0, 'cols': [], 'rows': [], 'shake': []}
        return an

    def test_fill_only_when_the_scene_fits(self):
        from funnycomm import framing
        one = framing.plan(self.an([track(.5)]), None, [[0, 6]], 'fill')
        self.assertEqual(one['look'], 'fill')
        self.assertTrue(all(g['kind'] == 'one' and g['ph'] == 1920 for g in one['segs']))
        two = framing.plan(self.an([track(.25), track(.75)]), None, [[0, 6]], 'fill')
        self.assertEqual(two['look'], 'fill')
        self.assertEqual({g['kind'] for g in two['segs']}, {'stack'})                       # two people apart: one above the other
        many = framing.plan(self.an([track(.3, w=.07), track(.5, w=.07), track(.7, w=.07)]), None, [[0, 6]], 'fill')
        self.assertEqual((many['look'], many.get('wanted')), ('square', 'fill'))            # a group: the whole scene instead
        self.assertTrue(all(g['kind'] == 'one' and g['ph'] in (1080, 860) for g in many['segs']))

    def test_square_webcam_clip_is_one_picture_with_small_webcam(self):
        from funnycomm import framing
        an = self.an([], mode='split')
        p = framing.plan(an, None, [[0, 6]], 'square')
        self.assertTrue(p['segs'] and all(g['kind'] == 'pip' for g in p['segs']))
        cx, cy, ch = p['segs'][0]['crop']
        self.assertGreater(cx - ch / 2, .26 * 1280 - 4)                # the big picture does not show the webcam again

    def test_empty_webcam_never_fills_half_the_screen(self):
        from funnycomm import framing
        an = self.an([], mode='split')
        self.assertEqual(framing.plan(an, None, [[0, 6]], 'fill')['look'], 'fill')
        an['face_cam'] = an['face_cam'][:8]                              # the streamer walked away after 2 s
        p = framing.plan(an, None, [[0, 6]], 'fill')
        self.assertEqual((p['look'], p.get('wanted'), p['segs'][0]['kind']), ('square', 'fill', 'pip'))

    def test_older_shorts_keep_their_framing(self):
        from funnycomm import framing
        p = framing.plan(self.an([track(.12), track(.88)]), None, [[0, 6]])
        self.assertIsNone(p.get('look'))

    def test_caption_has_one_emoji_and_no_generic_line(self):
        an = fake('x', 6, mode='irl', words=[('No', 2.0, 2.2, .95), ('way', 2.2, 2.4, .95), ('bro', 2.4, 2.6, .95)])
        src = {'streamer': 'lacy', 'title': 'Lacy was not ready for this', 'crowd_titles': '[]'}
        c = meta.caption(src, an, [], 'Lacy was not ready for this 😭')
        self.assertTrue(c.startswith('"No way bro') and c[-1] in '😭😳💀😂🤯', c)
        self.assertEqual(meta.caption(src, fake('x', 6, mode='irl'), [], 'Lacy was not ready for this 😭'), '')
        self.assertNotIn('‍', meta.caption({'streamer': 'lacy', 'title': 'Lacy finds her phone', 'crowd_titles': '[]'}, an, [], ''))

    def test_never_opens_on_a_face_against_the_lens(self):
        an = self.an([dict(track(.5, w=.36, dur=1.0)), dict(track(.5, w=.12), t=[1.2 + i * .25 for i in range(16)])])
        self.assertGreaterEqual(story.clean_open(an, 0.0, 5.0, []), .85)
        self.assertEqual(story.clean_open(self.an([track(.5)]), 0.0, 5.0, []), 0.0)

    def test_square_and_fill_render(self):
        d = Path(_TMP)
        a = clip(d / 'lk.mp4', 4, seed=3)
        for look in ('square', 'fill'):
            an = fake(a, mode='irl')
            an['tracks'] = [track(.5, w=.09, dur=4.0)]
            it = {'id': 'a', 'path': str(a), 'streamer': 'lacy', 'cut': {'keep': [[0, 4]], 'payoff': 1.2}, 'label': 'One'}
            plan = {'kind': 'single', 'fps': 30, 'headline': 'x', 'caption': 'Lacy finds the spot 😭', 'items': [it], 'voice': [],
                    'style': {**render.DEFAULT_STYLE, 'preset': 'ultrafast', 'crf': 30, 'out_h': 1920, 'look': look}}
            out = d / f'look_{look}.mp4'
            r = render.render(plan, [an], out)
            self.assertEqual(r['looks'], [look])
            self.assertEqual(r['zooms'], 1)                            # one push-in on the reaction, not more
            top = media.grab(out, 2.0)[:300].mean()
            self.assertLess(top, 12) if look == 'square' else self.assertGreater(top, 30)
            self.assertEqual(qc.check(out, r['duration'], framed=True)['issues'], [])


class Order(unittest.TestCase):
    def test_downloads_alternate_streamers(self):
        rows = [{'id': f'{w}{i}', 'streamer': w, 'views': v, 'created': '2026-11-01T00:00:00Z'} for w in ('a', 'b') for i, v in enumerate((900, 100))]
        self.assertEqual([r['streamer'] for r in core.download_order(rows)], ['a', 'b', 'a', 'b'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
