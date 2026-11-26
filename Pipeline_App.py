"""FunnyComm - permitted Twitch clips -> edited Shorts -> your review -> PRIVATE YouTube."""
from __future__ import annotations
import json, os, queue, secrets, subprocess, sys, threading, time, traceback, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, parse_qs, urlparse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import requests
from PIL import Image, ImageTk
import pipeline_core as core
import pipeline_youtube as ytlink
import pipeline_simple as simple
from pipeline_core import (DATA, SRC, REVIEW, USED, REPORTS, BACKUPS, ANALYSIS, settings, save_settings, Twitch, record_sources, source_rows, source,
                           production_rows, production, update_prod, update_source, download_new, import_local, upload_private, retry_thumbnails,
                           scan_existing_folder, import_skip_urls, recheck_downloads)
from funnycomm import VERSION, analyze, ears, index, longform, media, meta, speech, safety, topics, vision
import pipeline_render as pr

BG, PANEL, CARD, LINE = '#0f141c', '#161d28', '#1d2634', '#2a3646'
FG, DIM, GREEN, AMBER, RED, BLUE = '#e9eef5', '#93a1b5', '#2be38f', '#ffc247', '#ff5d6c', '#5aa9ff'
RISK = {'clear': ('Clear', GREEN), 'caution': ('Check', AMBER), 'hold': ('HELD', RED), None: ('-', DIM), '': ('-', DIM)}
STATE = {'discovered': 'Found', 'downloaded': 'Ready to edit', 'used': 'Used', 'existing': 'Already used', 'skipped': 'Excluded by you',
         'review': 'Needs your review', 'approved': 'Approved', 'uploaded_private': 'Uploaded (private)', 'failed': 'Failed', 'building': 'Building', 'rejected': 'Rejected'}


def style(root):
    s = ttk.Style(root)
    s.theme_use('clam')
    root.configure(bg=BG)
    f = ('Segoe UI', 10)
    s.configure('.', background=BG, foreground=FG, fieldbackground=CARD, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, font=f, troughcolor=PANEL)
    s.configure('TFrame', background=BG)
    s.configure('Panel.TFrame', background=PANEL)
    s.configure('Card.TFrame', background=CARD)
    s.configure('TLabel', background=BG, foreground=FG)
    s.configure('Panel.TLabel', background=PANEL)
    s.configure('Card.TLabel', background=CARD)
    s.configure('Dim.TLabel', foreground=DIM)
    s.configure('PDim.TLabel', background=PANEL, foreground=DIM)
    s.configure('CDim.TLabel', background=CARD, foreground=DIM)
    s.configure('H1.TLabel', font=('Segoe UI', 18, 'bold'))
    s.configure('H2.TLabel', font=('Segoe UI', 12, 'bold'), background=PANEL)
    s.configure('Big.TLabel', font=('Segoe UI', 24, 'bold'), background=CARD)
    s.configure('TButton', background=CARD, foreground=FG, padding=(11, 6), relief='flat', borderwidth=0)
    s.map('TButton', background=[('disabled', PANEL), ('active', LINE)], foreground=[('disabled', '#5b6675')])
    s.configure('Go.TButton', background=GREEN, foreground='#04130c', font=('Segoe UI', 11, 'bold'), padding=(18, 10))
    s.map('Go.TButton', background=[('disabled', '#25443a'), ('active', '#5cf0ad')], foreground=[('disabled', '#6c8a7e')])
    s.configure('Stop.TButton', background='#4a2329', foreground='#ffb9bf')
    s.map('Stop.TButton', background=[('disabled', PANEL), ('active', '#6b2f38')])
    s.configure('TNotebook', background=BG, borderwidth=0)
    s.configure('TNotebook.Tab', background=PANEL, foreground=DIM, padding=(18, 9), font=('Segoe UI', 10, 'bold'))
    s.map('TNotebook.Tab', background=[('selected', CARD)], foreground=[('selected', GREEN)], padding=[('selected', (18, 9))], expand=[('selected', (0, 0, 0, 0))])
    s.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=27, borderwidth=0)
    s.map('Treeview', background=[('selected', '#25507a')], foreground=[('selected', 'white')])
    s.configure('Treeview.Heading', background=CARD, foreground=DIM, relief='flat', font=('Segoe UI', 9, 'bold'))
    s.configure('TEntry', fieldbackground=CARD, foreground=FG, insertcolor=FG, padding=5)
    s.configure('TCombobox', fieldbackground=CARD, background=CARD, foreground=FG, arrowcolor=FG, padding=4)
    s.map('TCombobox', fieldbackground=[('readonly', CARD)], foreground=[('readonly', FG)])
    root.option_add('*TCombobox*Listbox.background', CARD)
    root.option_add('*TCombobox*Listbox.foreground', FG)
    s.configure('TCheckbutton', background=BG, foreground=FG)
    s.map('TCheckbutton', background=[('active', BG)], indicatorcolor=[('selected', GREEN), ('!selected', CARD)])
    s.configure('Panel.TCheckbutton', background=PANEL)
    s.map('Panel.TCheckbutton', background=[('active', PANEL)], indicatorcolor=[('selected', GREEN), ('!selected', CARD)])
    s.configure('Horizontal.TProgressbar', background=GREEN, troughcolor=CARD, borderwidth=0, thickness=10)
    s.configure('TScale', background=PANEL, troughcolor=CARD)
    s.configure('Vertical.TScrollbar', background=CARD, troughcolor=PANEL, arrowcolor=DIM, borderwidth=0)


def text_box(parent, **kw):
    return tk.Text(parent, bg=CARD, fg=FG, insertbackground=FG, relief='flat', wrap='word', padx=8, pady=6, font=('Segoe UI', 10),
                   highlightthickness=1, highlightbackground=LINE, highlightcolor=BLUE, **kw)


def scrolled(parent, columns, height=12, multi=True):
    wrap = ttk.Frame(parent, style='Panel.TFrame')
    tree = ttk.Treeview(wrap, columns=[c[0] for c in columns], show='headings', height=height, selectmode='extended' if multi else 'browse')
    bar = ttk.Scrollbar(wrap, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    for key, title, width, anchor in columns:
        tree.heading(key, text=title, anchor=anchor)
        tree.column(key, width=width, anchor=anchor, stretch=key in ('title', 'why'))
    tree.pack(side='left', fill='both', expand=True)
    bar.pack(side='right', fill='y')
    for k, (_, col) in RISK.items():
        tree.tag_configure(f'risk_{k}', foreground=col if k in ('hold', 'caution') else FG)
    return wrap, tree


class Tip:
    """Simple hover help."""

    def __init__(self, w, text):
        self.w, self.text, self.t = w, text, None
        w.bind('<Enter>', self.show)
        w.bind('<Leave>', self.hide)

    def show(self, e=None):
        self.t = tk.Toplevel(self.w)
        self.t.wm_overrideredirect(True)
        self.t.geometry(f'+{self.w.winfo_rootx() + 12}+{self.w.winfo_rooty() + self.w.winfo_height() + 4}')
        tk.Label(self.t, text=self.text, bg='#000', fg=FG, padx=8, pady=4, justify='left', wraplength=380, font=('Segoe UI', 9)).pack()

    def hide(self, e=None):
        if self.t:
            self.t.destroy()
            self.t = None


# ======================================================================= app
class Pipeline:
    def __init__(self, root):
        self.root = root
        root.title(f'FunnyComm Studio {VERSION}')
        w, h = min(1500, root.winfo_screenwidth() - 60), min(940, root.winfo_screenheight() - 90)
        root.geometry(f'{w}x{h}+20+20')
        root.minsize(1080, 680)
        style(root)
        self.q = queue.Queue()
        self.busy = False
        self.controls = []
        self.images = {}
        core.migrate()
        top = ttk.Frame(root, padding=(18, 12, 18, 6))
        top.pack(fill='x')
        ttk.Label(top, text='FunnyComm', style='H1.TLabel').pack(side='left')
        ttk.Label(top, text='  make  →  review  →  post', style='Dim.TLabel').pack(side='left', pady=(8, 0))
        for label, fn in (('Settings', self.open_settings), ('Connect YouTube', self.connect_youtube)):
            b = ttk.Button(top, text=label, command=fn)
            b.pack(side='right', padx=3)
        self.yt_text = tk.StringVar()
        self.yt_label = ttk.Label(top, textvariable=self.yt_text, style='Dim.TLabel')
        self.yt_label.pack(side='right', padx=8)
        self.newer = False
        bottom = ttk.Frame(root, padding=(18, 4, 18, 12))
        bottom.pack(side='bottom', fill='x')
        self.pbar = ttk.Progressbar(bottom, maximum=100)
        self.pbar.pack(fill='x', pady=(0, 5))
        row = ttk.Frame(bottom)
        row.pack(fill='x')
        self.status = tk.StringVar(value='Ready.')
        ttk.Label(row, textvariable=self.status, style='Dim.TLabel').pack(side='left', fill='x', expand=True)
        self.stop_b = ttk.Button(row, text='Stop', style='Stop.TButton', command=self.stop, state='disabled')
        self.stop_b.pack(side='right')
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill='both', expand=True, padx=18, pady=4)
        self.home = simple.Home(self)
        self.review = simple.Review(self)
        self.pick = simple.Pick(self)
        more = self.more = ttk.Frame(self.nb, padding=4)
        main, self.nb = self.nb, ttk.Notebook(more)          # the detailed screens live under More
        self.nb.pack(fill='both', expand=True)
        self.details = Review(self)
        self.clips = Clips(self)
        self.videos = Videos(self)
        self.library = Library(self)
        self.nb2, self.nb = self.nb, main
        for tab, name in ((self.details, 'Detailed review'), (self.clips, 'Downloaded clips'), (self.library, 'Library & tools')) + (((self.videos, 'YouTube videos'),) if settings().get('show_videos') else ()):
            self.nb2.add(tab.frame, text=name)
        for fr, name in ((self.home.frame, 'Home'), (self.review.frame, 'Review'), (self.pick.frame, 'Get clips'), (more, 'More')):
            self.nb.add(fr, text=name)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(120, self.poll)
        root.after(4000, self.watch_version)
        threading.Thread(target=self._poster, daemon=True).start()
        self.refresh()
        threading.Thread(target=self._startup, daemon=True).start()

    # ---------------------------------------------------------------- plumbing
    def _startup(self):
        try:
            core.daily_backup()
            n = core.drive_warnings()
            if n:
                self.log(f'Drive notice: Windows logged {n} disk warning(s) in the last 24 h. Check the USB connection and keep backups before long jobs.')
            media.tool('ffmpeg')
            miss = [n for n, ok in (('speech recognition', speech.installed()), ('nudity scanner', safety.nude_ready()), ('face model', vision.face_model_ready()), ('laugh detector', ears.tagger_ready()), ('chat reader', ears.ocr_ready())) if not ok]
            if miss:
                self.log('Not installed: ' + ', '.join(miss) + '. Run Install_Smart_Editing.bat for full quality.')
        except Exception as e:
            self.log('Startup check: ' + str(e))

    def _poster(self):
        """Uploads approved Shorts in the background, whatever else is going on."""
        time.sleep(8)
        while True:
            did = False
            try:
                did = ytlink.post_next(settings(), self.log, lambda m: self.q.put(('post', m)))
                if did:
                    self.q.put(('refresh', None))
                if did and settings().get('auto_free') and not self.busy:
                    pass
            except RuntimeError as e:
                if 'already running' not in str(e) and 'busy' not in str(e):
                    self.log('UPLOAD ERROR ' + str(e)[:200])
            except Exception as e:
                self.log('UPLOAD ERROR ' + str(e)[:200])
                time.sleep(120)
            time.sleep(6 if did else 30)

    def open_details(self, pid=None):
        self.nb.select(self.more)
        self.nb2.select(self.details.frame)
        p = pid and production(pid)
        if p:
            self.details.flt.set(STATE.get(p['state'], 'All'))
            self.details.refresh()
            if self.details.tree.exists(pid):
                self.details.tree.selection_set(pid)
                self.details.tree.see(pid)

    def log(self, m):
        self.q.put(('log', str(m)))

    def say(self, m):
        self.q.put(('status', str(m)))

    def progress(self, a, b, m=''):
        self.q.put(('prog', (100 * a / max(b, 1e-6), m)))

    def work(self, fn, label='Working...', lock=True):
        if self.busy:
            messagebox.showinfo('Busy', 'A job is already running. Wait for it or press Stop.')
            return
        self.busy = True
        media.clear_cancel()
        self.status.set(label)
        self.stop_b.configure(state='normal')
        for b in self.controls:
            b.configure(state='disabled')

        def runner():
            try:
                if lock:
                    with core.RunLock():
                        res = fn()
                else:
                    res = fn()
                self.q.put(('finish', str(res) if res is not None else 'Done.'))
            except media.Cancelled:
                self.q.put(('finish', 'Stopped. Everything finished so far is kept.'))
            except Exception as e:
                try:
                    with (REPORTS / 'errors.log').open('a', encoding='utf-8') as f:
                        f.write(time.strftime('%F %T ') + traceback.format_exc() + '\n')
                except OSError:
                    pass
                self.q.put(('error', str(e)))
        threading.Thread(target=runner, daemon=True).start()

    def stop(self):
        media.request_cancel()
        self.status.set('Stopping after the current step...')

    def poll(self):
        try:
            while True:
                t, v = self.q.get_nowait()
                if t == 'log':
                    self.home.add_log(v)
                elif t == 'status':
                    self.status.set(v)
                elif t == 'prog':
                    self.pbar['value'] = v[0]
                    if v[1]:
                        self.status.set(v[1])
                elif t == 'refresh':
                    self.refresh()
                elif t == 'post':
                    if not self.busy:
                        self.status.set(v or 'Ready.')
                elif t == 'call':
                    v()
                elif t in ('finish', 'error'):
                    self.busy = False
                    self.pbar['value'] = 0
                    self.stop_b.configure(state='disabled')
                    for b in self.controls:
                        b.configure(state='normal')
                    self.status.set(v if t == 'finish' else 'Problem: ' + v[:200])
                    self.home.add_log(('DONE: ' if t == 'finish' else 'ERROR: ') + v)
                    self.refresh()
                    if t == 'error':
                        messagebox.showerror('Something went wrong', v[:900])
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def watch_version(self):
        """Say so when an update was installed while this window stayed open."""
        try:
            txt = (core.ROOT / 'funnycomm' / '__init__.py').read_text(encoding='utf-8')
            disk = txt.split("VERSION = '")[1].split("'")[0]
        except (OSError, IndexError):
            disk = VERSION
        if disk != VERSION and not self.newer:
            self.newer = True
            self.root.title(f'FunnyComm Studio {VERSION}  -  UPDATE {disk} INSTALLED: close and reopen')
            self.log(f'NOTE: version {disk} was installed. Close FunnyComm and open it again to use it.')
            self.status.set(f'Version {disk} was installed. Close FunnyComm and open it again to use it.')
        self.root.after(60000, self.watch_version)

    def connect_youtube(self, after=None):
        w = getattr(self, 'yt_window', None)
        if w and w.winfo_exists():
            w.lift()
            return
        self.yt_window = ConnectYouTube(self, after)

    def yt_refresh(self):
        s = ytlink.status(settings())
        self.yt_text.set('YouTube: ' + (s['channel'] or {}).get('title', 'connected') if s['ready'] else 'YouTube: not connected')
        self.yt_label.configure(foreground=GREEN if s['ready'] else AMBER)
        return s

    def refresh(self):
        self.yt_refresh()
        self.home.refresh()
        self.review.refresh()
        self.pick.refresh()
        self.details.refresh()
        self.clips.refresh()
        self.videos.refresh()
        self.library.refresh()

    def close(self):
        if self.busy:
            if not messagebox.askyesno('Job running', 'A job is still running. Stop it and close?\n\nFinished Shorts and downloads are kept; the current one can be rebuilt.'):
                return
            media.request_cancel()
            self.root.after(1500, self.root.destroy)
            return
        self.root.destroy()

    def open_path(self, p):
        p = Path(p)
        if p.exists():
            os.startfile(str(p)) if os.name == 'nt' else subprocess.Popen(['xdg-open', str(p)])

    def button(self, parent, text, fn, style='TButton', tip=None, managed=True, **pack):
        b = ttk.Button(parent, text=text, command=fn, style=style)
        b.pack(**({'side': 'left', 'padx': 3} | pack))
        if managed:
            self.controls.append(b)
        if tip:
            Tip(b, tip)
        return b

    def photo(self, path, box):
        """Cached, scaled image or None."""
        path = Path(path or '')
        if not path.is_file():
            return None
        key = (str(path), path.stat().st_mtime_ns, box)
        if key not in self.images:
            if len(self.images) > 60:
                self.images.clear()
            im = Image.open(path)
            im.thumbnail(box)
            self.images[key] = ImageTk.PhotoImage(im)
        return self.images[key]

    def open_settings(self):
        ConfigWindow(self.root, self.refresh)

    # -------------------------------------------------------------------- jobs
    def job_index(self, logins=None):
        c = settings()
        logins = logins or list(dict.fromkeys(c['streamers']))
        n = index.update(Twitch(c), logins, int(c.get('index_days', 180)), self.log, self.progress)
        return f'{n:,} clips indexed'

    def job_discover(self):
        """Index first, then take the moments most people clipped (not the most viewed clips)."""
        c = settings()
        if not c['streamers']:
            raise RuntimeError('Add at least one Twitch streamer in Settings.')
        self.job_index()
        ms = index.pick_for_shorts(c['streamers'], int(c['discover_days']), int(c['max_clips_per_streamer']), self.log)
        record_sources([index.to_source(m['best'], m) for m in ms])
        self.q.put(('refresh', None))
        return f'{len(ms)} crowd-picked moments found'

    def job_download(self):
        n = download_new(settings(), self.log, lambda a, b: self.progress(a, b, f'Downloading {a + 1} of {b}'))
        self.q.put(('refresh', None))
        return f'{n} clips downloaded'

    def job_analyse(self):
        n = pr.analyse_all(settings(), self.log, lambda a, b: self.progress(a, b, f'Analysing clip {min(a + 1, b)} of {b}'))
        return f'{n} clips analysed'

    def job_build(self):
        n = pr.make_shorts(settings(), self.log, self.progress)
        return f'{n} Shorts are waiting in Review & approve'

    def job_all(self):
        if not settings().get('permission_confirmed'):
            raise RuntimeError('Open Settings and confirm you have permission to use this footage first.')
        self.log(self.job_discover())
        self.log(self.job_download())
        recheck_downloads(self.log)
        return self.job_build()


# ====================================================================== home
class Home:
    def __init__(self, app):
        self.app = app
        f = self.frame = ttk.Frame(app.nb, padding=16)
        row = ttk.Frame(f)
        row.pack(fill='x')
        app.button(row, '▶  Make Shorts now', lambda: app.work(app.job_all, 'Finding, downloading and editing...'), 'Go.TButton',
                   'Finds new permitted clips, skips everything you already used, downloads, edits and puts finished Shorts in your review queue.')
        ttk.Label(row, text='   or one step at a time:', style='Dim.TLabel').pack(side='left')
        app.button(row, '1  Find clips', lambda: app.work(app.job_discover, 'Searching Twitch...'))
        app.button(row, '2  Download', lambda: app.work(app.job_download, 'Downloading...'))
        app.button(row, '3  Analyse', lambda: app.work(app.job_analyse, 'Analysing...'))
        app.button(row, '4  Build Shorts', lambda: app.work(app.job_build, 'Editing...'))
        cards = ttk.Frame(f)
        cards.pack(fill='x', pady=14)
        self.cards = {}
        for k, name in (('found', 'Clips waiting to download'), ('ready', 'Ready to edit'), ('held', 'Held for safety'), ('dupes', 'Repeats skipped'),
                        ('review', 'Shorts to review'), ('approved', 'Approved'), ('private', 'Uploaded private')):
            c = ttk.Frame(cards, style='Card.TFrame', padding=(14, 10))
            c.pack(side='left', fill='x', expand=True, padx=4)
            v = tk.StringVar(value='0')
            ttk.Label(c, textvariable=v, style='Big.TLabel').pack(anchor='w')
            ttk.Label(c, text=name, style='CDim.TLabel').pack(anchor='w')
            self.cards[k] = v
        self.next = tk.StringVar()
        ttk.Label(f, textvariable=self.next, font=('Segoe UI', 12, 'bold'), foreground=GREEN, wraplength=1200).pack(anchor='w', pady=(0, 8))
        ttk.Label(f, text='Activity', style='Dim.TLabel').pack(anchor='w')
        self.box = text_box(f, height=14)
        self.box.pack(fill='both', expand=True, pady=(4, 0))
        self.box.tag_configure('bad', foreground=RED)
        self.box.tag_configure('warn', foreground=AMBER)
        self.box.tag_configure('good', foreground=GREEN)

    def add_log(self, m):
        tag = 'bad' if m.startswith(('ERROR', 'FAILED', 'BUILD FAILED', 'UPLOAD ERROR')) else 'warn' if m.startswith(('HELD', 'SKIPPED', 'SAME', 'ALREADY', 'Drive', 'NOTE', 'Not installed')) else 'good' if m.startswith(('READY', 'DONE')) else ''
        self.box.insert('end', time.strftime('%H:%M  ') + m + '\n', tag)
        self.box.see('end')

    def refresh(self):
        s, p = source_rows(), production_rows()
        n = {'found': sum(x['state'] == 'discovered' and x['source_kind'] == 'clip' for x in s), 'ready': sum(x['state'] == 'downloaded' and (x['risk'] != 'hold' or x['allow_hold']) for x in s),
             'held': sum(x['risk'] == 'hold' and x['state'] in ('downloaded', 'discovered') and not x['allow_hold'] for x in s), 'dupes': sum(x['state'] == 'existing' for x in s),
             'review': sum(x['state'] == 'review' for x in p), 'approved': sum(x['state'] == 'approved' for x in p), 'private': sum(x['state'] == 'uploaded_private' for x in p)}
        for k, v in n.items():
            self.cards[k].set(str(v))
        c = settings()
        if not c['streamers'] or not c['twitch_client_id']:
            m = 'Next: open Settings and add your Twitch app and streamers.'
        elif not c.get('permission_confirmed'):
            m = 'Next: confirm footage permission in Settings.'
        elif n['review']:
            m = f'Next: watch {n["review"]} Short(s) in "Review & approve".'
        elif n['approved'] and not ytlink.status(c)['ready']:
            m = f'Next: press "Connect YouTube" (top right) once, so your {n["approved"]} approved item(s) can upload privately.'
        elif n['approved']:
            m = f'Next: upload {n["approved"]} approved item(s) privately from "Review & approve".'
        else:
            m = 'Next: press "Make Shorts now".'
        self.next.set(m)


# ===================================================================== clips
class Clips:
    def __init__(self, app):
        self.app = app
        f = self.frame = ttk.Frame(app.nb, padding=12)
        bar = ttk.Frame(f)
        bar.pack(fill='x', pady=(0, 8))
        self.flt = tk.StringVar(value='Ready to edit')
        cb = ttk.Combobox(bar, textvariable=self.flt, state='readonly', width=20,
                          values=['Ready to edit', 'Held for safety', 'Found', 'Already used', 'Used', 'Excluded by you', 'Failed', 'All'])
        cb.pack(side='left')
        cb.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        self.who = tk.StringVar(value='All streamers')
        self.whobox = ttk.Combobox(bar, textvariable=self.who, state='readonly', width=16)
        self.whobox.pack(side='left', padx=6)
        self.whobox.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        app.button(bar, 'Import local videos', self.import_files)
        app.button(bar, 'Download selected', self.download)
        app.button(bar, 'Analyse selected again', self.reanalyse)
        app.button(bar, 'Build single from selected', self.build_single)
        app.button(bar, 'Build ranking from selected', self.build_ranking, tip='Select 2-5 clips. They are ordered by score so the strongest ends up #1.')
        pane = ttk.Panedwindow(f, orient='horizontal')
        pane.pack(fill='both', expand=True)
        left, self.tree = scrolled(pane, [('score', 'Score', 58, 'center'), ('risk', 'Safety', 66, 'center'), ('streamer', 'Streamer', 86, 'w'), ('title', 'Twitch title', 250, 'w'),
                                          ('sec', 'Length', 60, 'center'), ('views', 'Views', 72, 'e'), ('layout', 'Layout', 74, 'center'), ('rate', 'Rating', 64, 'center'), ('state', 'Status', 116, 'w')])
        pane.add(left, weight=3)
        right = ttk.Frame(pane, style='Panel.TFrame', padding=12)
        pane.add(right, weight=2)
        self.img = tk.Label(right, bg=PANEL)
        self.img.pack(fill='x')
        self.head = tk.StringVar()
        ttk.Label(right, textvariable=self.head, style='H2.TLabel', wraplength=520).pack(anchor='w', pady=(8, 4))
        self.info = text_box(right, height=13)
        self.info.pack(fill='both', expand=True)
        for t, c in (('hold', RED), ('caution', AMBER), ('info', DIM), ('h', BLUE)):
            self.info.tag_configure(t, foreground=c)
        r1 = ttk.Frame(right, style='Panel.TFrame')
        r1.pack(fill='x', pady=(8, 2))
        ttk.Label(r1, text='Your rating', style='PDim.TLabel').pack(side='left')
        for k in range(0, 6):
            b = ttk.Button(r1, text='none' if not k else '★' * k, command=lambda k=k: self.rate(k), width=5 + (k > 2) * (k - 2))
            b.pack(side='left', padx=1)
        r2 = ttk.Frame(right, style='Panel.TFrame')
        r2.pack(fill='x', pady=2)
        app.button(r2, 'Play clip', self.play, managed=False)
        app.button(r2, 'Open on Twitch', self.open_web, managed=False)
        app.button(r2, 'Fix webcam / layout', self.fix_layout, managed=False, tip='Draw the webcam box or force a layout when detection got it wrong.')
        r3 = ttk.Frame(right, style='Panel.TFrame')
        r3.pack(fill='x', pady=2)
        app.button(r3, 'Exclude', lambda: self.set_state('skipped'), managed=False)
        app.button(r3, 'Use anyway', self.use_anyway, managed=False, tip='Releases a held, excluded or already-used clip after you have checked it yourself.')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.show())
        self.tree.bind('<Double-1>', lambda e: self.play())

    def refresh(self):
        rows = source_rows()
        names = sorted({r['streamer'] for r in rows})
        self.whobox['values'] = ['All streamers'] + names
        keep = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        want = self.flt.get()
        for r in sorted(rows, key=lambda r: -(r['score'] or 0)):
            if r['source_kind'] == 'highlight' and want != 'All':
                continue
            if self.who.get() != 'All streamers' and r['streamer'] != self.who.get():
                continue
            held = r['risk'] == 'hold' and not r['allow_hold'] and r['state'] in ('downloaded', 'discovered')
            ok = {'Ready to edit': r['state'] == 'downloaded' and not held, 'Held for safety': held, 'Found': r['state'] == 'discovered' and not held, 'Already used': r['state'] == 'existing',
                  'Used': r['state'] == 'used', 'Excluded by you': r['state'] == 'skipped', 'Failed': bool(r['error']), 'All': True}[want]
            if not ok:
                continue
            lay = ''
            if r['risk']:
                an = analyze.load(ANALYSIS, r['id'])
                lay = {'split': 'Webcam', 'irl': 'IRL', 'scene': 'Action', 'portrait': 'Portrait'}.get(an['layout']['mode'], '') if an else ''
            self.tree.insert('', 'end', iid=r['id'], tags=(f"risk_{r['risk']}",), values=(
                f"{r['score']:.0f}" if r['score'] is not None else '-', RISK.get(r['risk'], RISK[None])[0], r['streamer'], r['title'], f"{r['seconds'] or 0:.0f}s", f"{r['views'] or 0:,}",
                lay, '★' * (r['rating'] or 0), 'Held' if held else STATE.get(r['state'], r['state'])))
        self.tree.selection_set([k for k in keep if self.tree.exists(k)])
        self.show()

    def sel(self):
        return [source(i) for i in self.tree.selection()]

    def show(self):
        rows = self.sel()
        self.info.delete('1.0', 'end')
        if not rows:
            self.head.set('Select a clip to see why it scored the way it did.')
            self.img.configure(image='')
            return
        s = rows[0]
        self.head.set(f"{s['streamer']}: {s['title']}" + (f'   (+{len(rows) - 1} more selected)' if len(rows) > 1 else ''))
        im = self.app.photo(analyze.cache_path(ANALYSIS, s['id']).with_suffix('.jpg'), (560, 330))
        self.img.configure(image=im or '')
        self.img.image = im
        put = lambda t, tag='': self.info.insert('end', t + '\n', tag)
        if s.get('note'):
            put(s['note'], 'caution')
        if s.get('error'):
            put('Problem: ' + s['error'][:260], 'hold')
        if s.get('score_json'):
            sc = json.loads(s['score_json'])
            put(f"Score {sc['total']:.0f}: " + (', '.join(sc['why']) or 'nothing stands out'), 'h')
            put('  ' + '   '.join(f'{k} {v:.0f}' for k, v in sc['parts'].items()), 'info')
        if s.get('risk_json'):
            put('\nSafety screen (advisory)', 'h')
            rs = json.loads(s['risk_json'])
            for r in rs:
                at = '  @ ' + ', '.join(f'{a:.0f}-{b:.0f}s' for a, b in r['t'][:4]) if r['t'] else ''
                put('  ' + r['text'] + at, r['level'])
            if not rs:
                put('  Nothing detected. Still needs your eyes and ears.', 'info')
        an = analyze.load(ANALYSIS, s['id'])
        if an:
            m = an['moments']
            put(f"\nPayoff around {m['payoff']:.1f}s" + (f"   Game: {s['game']}" if s.get('game') else ''), 'h')
            for ln in an['speech']['lines'][:14]:
                put(f'  {ln[0]:5.1f}s  {ln[2]}', '')

    def import_files(self):
        paths = filedialog.askopenfilenames(title='Add your permitted clips', filetypes=[('Video', '*.mp4 *.mov *.mkv *.webm'), ('All', '*.*')])
        if not paths:
            return
        names = settings()['streamers']
        who = AskText(self.app.root, 'Which streamer is this?', names[0] if names else 'local').value
        if who is None:
            return

        def job():
            for p in paths:
                self.app.log('Imported ' + import_local(p, who.strip().lower() or 'local'))
            return f'{len(paths)} file(s) imported'
        self.app.work(job, 'Importing...')

    def download(self):
        ids = [s['id'] for s in self.sel() if s['state'] == 'discovered']
        if ids:
            self.app.work(lambda: f'{download_new(settings(), self.app.log, lambda a, b: self.app.progress(a, b), only=set(ids))} downloaded', 'Downloading...')

    def reanalyse(self):
        rows = [s for s in self.sel() if s['path'] and Path(s['path']).is_file()]
        if not rows:
            return

        def job():
            for n, s in enumerate(rows):
                self.app.progress(n, len(rows), 'Analysing ' + (s['title'] or '')[:40])
                pr.analyse_source(s, settings(), force=True)
            return f'{len(rows)} analysed'
        self.app.work(job)

    def build_single(self):
        rows = [s for s in self.sel() if s['state'] in ('downloaded', 'used') and s['path'] and Path(s['path']).is_file()]
        if not self._held_ok(rows):
            return

        def job():
            ok = 0
            for n, s in enumerate(rows):
                if pr.build_one([s], 'single', settings(), self.app.log, progress=lambda p, m='', n=n: self.app.progress(n + p / 100, len(rows), f'Editing {n + 1}/{len(rows)}: {m}')):
                    pr.archive_used([s])
                    ok += 1
            return f'{ok} Short(s) built' + ('' if ok == len(rows) else ' (others already exist in Review)')
        if rows:
            self.app.work(job)

    def build_ranking(self):
        rows = [s for s in self.sel() if s['state'] in ('downloaded', 'used') and s['path'] and Path(s['path']).is_file()]
        if not 2 <= len(rows) <= 5:
            messagebox.showinfo('Ranking', 'Select 2 to 5 downloaded clips.')
            return
        if not self._held_ok(rows):
            return

        def job():
            pr.analyse_all(settings(), self.app.log, ids={r['id'] for r in rows})
            fresh = sorted([source(r['id']) for r in rows], key=lambda r: r['score'] or 0)
            if pr.build_one(fresh, 'ranking', settings(), self.app.log, progress=lambda p, m='': self.app.progress(p, 100, m)):
                pr.archive_used(fresh)
                return 'Ranking built'
            return 'Not built: the same ranking already exists, or it failed (see log).'
        self.app.work(job)

    def _held_ok(self, rows):
        if any(r['risk'] == 'hold' and not r['allow_hold'] for r in rows):
            messagebox.showwarning('Held clip', 'One of these clips is held for safety. Check it and press "Use anyway" first.')
            return False
        return True

    def rate(self, k):
        for s in self.sel():
            update_source(s['id'], rating=k)
            an = analyze.load(ANALYSIS, s['id'])
            if an and s['path'] and Path(s['path']).is_file():
                pr.analyse_source(source(s['id']), settings())
        self.refresh()

    def play(self):
        s = (self.sel() or [None])[0]
        if s and s['path']:
            self.app.open_path(s['path'])

    def open_web(self):
        s = (self.sel() or [None])[0]
        if s and str(s['url']).startswith('https://'):
            webbrowser.open(s['url'])

    def set_state(self, st):
        for s in self.sel():
            if s['state'] in ('discovered', 'downloaded'):
                update_source(s['id'], state=st, note='Excluded by you')
        self.refresh()

    def use_anyway(self):
        rows = self.sel()
        if not rows:
            return
        if any(s['risk'] == 'hold' for s in rows) and not messagebox.askyesno(
                'Release held clip', 'This clip was held because it may break YouTube rules (see the safety list).\n\nYour channel already has two strikes. Have you watched it fully and decided it is acceptable to edit and review?'):
            return
        for s in rows:
            has = s['path'] and Path(s['path']).is_file() and Path(s['path']).parent.resolve() in (SRC.resolve(), USED.resolve())
            st = s['state']
            if st in ('existing', 'skipped'):
                st = 'downloaded' if has else 'discovered'
            update_source(s['id'], state=st, allow_hold=1, note='Use anyway (checked by you)')
            with core.db() as con:
                con.execute('DELETE FROM clip_skip WHERE id=?', (s['id'],))
        self.refresh()

    def fix_layout(self):
        s = (self.sel() or [None])[0]
        if s and s['path'] and Path(s['path']).is_file():
            LayoutWindow(self.app, s)


class AskText(tk.Toplevel):
    def __init__(self, root, prompt, initial=''):
        super().__init__(root, bg=BG)
        self.value = None
        self.title(prompt)
        fr = ttk.Frame(self, padding=16)
        fr.pack()
        ttk.Label(fr, text=prompt).pack(anchor='w')
        v = tk.StringVar(value=initial)
        e = ttk.Entry(fr, textvariable=v, width=44)
        e.pack(pady=8)
        e.focus_set()

        def ok(_=None):
            self.value = v.get()
            self.destroy()
        e.bind('<Return>', ok)
        ttk.Button(fr, text='OK', command=ok).pack(anchor='e')
        self.transient(root)
        self.grab_set()
        root.wait_window(self)


class LayoutWindow(tk.Toplevel):
    """Draw the webcam or force a layout for one clip."""

    def __init__(self, app, s):
        super().__init__(app.root, bg=BG)
        self.app, self.s = app, s
        self.title('Layout: ' + (s['title'] or s['id'])[:60])
        an = analyze.load(ANALYSIS, s['id'])
        dur = an['meta']['duration'] if an else 10
        self.over = json.loads(s['layout_json']) if s.get('layout_json') else {}
        cam = self.over.get('cam') or (an and an['layout']['cam'] and an['layout']['cam']['rect'])
        self.rect = list(cam) if cam else None
        self.W = 960
        self.cv = tk.Canvas(self, width=self.W, height=540, bg='black', highlightthickness=0)
        self.cv.pack(padx=12, pady=12)
        self.cv.bind('<Button-1>', self.down)
        self.cv.bind('<B1-Motion>', self.move)
        row = ttk.Frame(self, padding=(12, 0, 12, 12))
        row.pack(fill='x')
        self.t = tk.DoubleVar(value=(an['moments']['payoff'] if an else dur / 2))
        ttk.Label(row, text='Time').pack(side='left')
        ttk.Scale(row, from_=0, to=max(.2, dur - .1), variable=self.t, length=240, command=lambda v: self.load_soon()).pack(side='left', padx=6)
        self.mode = tk.StringVar(value=self.over.get('mode') or 'auto')
        for k, n in (('auto', 'Automatic'), ('split', 'Webcam + game'), ('irl', 'Follow people'), ('scene', 'Follow action')):
            ttk.Radiobutton(row, text=n, value=k, variable=self.mode).pack(side='left', padx=4)
        self.pay = tk.BooleanVar(value=self.over.get('payoff') is not None)
        ttk.Checkbutton(row, text='Use this time as the payoff', variable=self.pay).pack(side='left', padx=8)
        ttk.Button(row, text='Save', style='Go.TButton', command=self.save).pack(side='right')
        ttk.Button(row, text='Reset', command=self.reset).pack(side='right', padx=4)
        ttk.Label(self, text='Drag a box around the webcam. The same box is tried first on this streamer\'s other clips.', style='Dim.TLabel').pack(pady=(0, 10))
        self._job = None
        self.load()

    def load_soon(self):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(180, self.load)

    def load(self):
        import cv2
        try:
            fr = media.grab(self.s['path'], self.t.get(), self.W)
        except media.MediaError:
            return
        self.H = fr.shape[0]
        self.cv.configure(height=self.H)
        self.ph = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
        self.cv.delete('all')
        self.cv.create_image(0, 0, image=self.ph, anchor='nw')
        self.draw()

    def draw(self):
        self.cv.delete('r')
        if self.rect:
            x, y, w, h = self.rect
            self.cv.create_rectangle(x * self.W, y * self.H, (x + w) * self.W, (y + h) * self.H, outline=GREEN, width=3, tags='r')

    def down(self, e):
        self.a = (e.x / self.W, e.y / self.H)

    def move(self, e):
        x, y = min(1, max(0, e.x / self.W)), min(1, max(0, e.y / self.H))
        self.rect = [min(self.a[0], x), min(self.a[1], y), abs(self.a[0] - x), abs(self.a[1] - y)]
        self.mode.set('split')
        self.draw()

    def reset(self):
        update_source(self.s['id'], layout_json=None)
        self.destroy()
        self.app.refresh()

    def save(self):
        o = {}
        if self.mode.get() != 'auto':
            o['mode'] = self.mode.get()
        if self.mode.get() == 'split' and self.rect and self.rect[2] > .04 and self.rect[3] > .04:
            o['cam'] = [round(v, 5) for v in self.rect]
        if self.pay.get():
            o['payoff'] = round(self.t.get(), 2)
        update_source(self.s['id'], layout_json=json.dumps(o) if o else None)
        self.destroy()
        self.app.refresh()


# ==================================================================== videos
class Videos:
    """Plain-English idea -> 8-12 minute 16:9 video."""

    def __init__(self, app):
        self.app = app
        f = self.frame = ttk.Frame(app.nb, padding=14)
        top = ttk.Frame(f, style='Panel.TFrame', padding=14)
        top.pack(fill='x')
        ttk.Label(top, text='What video do you want?', style='H2.TLabel').pack(anchor='w')
        row = ttk.Frame(top, style='Panel.TFrame')
        row.pack(fill='x', pady=6)
        self.idea = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.idea, font=('Segoe UI', 12))
        e.pack(side='left', fill='x', expand=True)
        e.bind('<Return>', lambda _: self.preview())
        ttk.Label(row, text='  Minutes', style='PDim.TLabel').pack(side='left')
        self.mins = tk.StringVar(value=str(settings().get('video_minutes', 10)))
        ttk.Entry(row, textvariable=self.mins, width=4).pack(side='left', padx=4)
        app.button(row, 'Find clips', self.preview, managed=False)
        app.button(row, '▶  Build this video', self.build, 'Go.TButton', 'Downloads the best matching moments, checks safety, cuts, orders and renders. About 25-40 minutes. Nothing uploads.')
        ttk.Label(top, text='Examples: Marlon rizzing up girls   •   best of marlon in the last week   •   last week in the core house   •   best of lacy   •   core house pranks   •   ron crashing out last month',
                  style='PDim.TLabel').pack(anchor='w')
        self.info = tk.StringVar()
        ttk.Label(top, textvariable=self.info, background=PANEL, foreground=GREEN, wraplength=1250, justify='left').pack(anchor='w', pady=(6, 0))
        pane = ttk.Panedwindow(f, orient='horizontal')
        pane.pack(fill='both', expand=True, pady=10)
        left = ttk.Frame(pane)
        pane.add(left, weight=2)
        bar = ttk.Frame(left)
        bar.pack(fill='x', pady=(0, 6))
        ttk.Label(bar, text='Ideas your clips can support', font=('Segoe UI', 11, 'bold')).pack(side='left')
        app.button(bar, 'Update clip index', self.update_index, tip='Reads clip details (no video) for every permitted channel, 180 days. Takes 1-2 minutes the first time.')
        app.button(bar, 'Refresh ideas', lambda: self.load_ideas(True), managed=False)
        w1, self.ideas = scrolled(left, [('pot', 'Potential', 72, 'center'), ('title', 'Video', 250, 'w'), ('n', 'Moments', 70, 'center'), ('min', 'Max min', 66, 'center'), ('why', 'Why', 240, 'w')], multi=False)
        w1.pack(fill='both', expand=True)
        self.ideas.bind('<<TreeviewSelect>>', lambda _: self.take())
        self.ideas.bind('<Double-1>', lambda _: self.preview())
        right = ttk.Frame(pane)
        pane.add(right, weight=3)
        ttk.Label(right, text='Matching moments (best first)', font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(4, 6))
        w2, self.cands = scrolled(right, [('ppl', 'Clipped by', 76, 'center'), ('who', 'Streamer', 96, 'w'), ('date', 'Date', 62, 'w'), ('title', 'What viewers called it', 260, 'w'), ('views', 'Views', 64, 'e'), ('sec', 'Sec', 42, 'center'), ('fit', 'Fit', 50, 'center')])
        w2.pack(fill='both', expand=True)
        self.cands.bind('<Double-1>', lambda _: self.open_web())
        b = ttk.Frame(right)
        b.pack(fill='x', pady=4)
        app.button(b, 'Watch on Twitch', self.open_web, managed=False)
        app.button(b, 'Never use selected', self.exclude, managed=False)
        self.rows, self.found, self.idea_rows = [], [], []
        self.loaded = False

    def refresh(self):
        if not self.loaded:
            self.loaded = True
            self.load_ideas()

    def channels(self):
        c = settings()
        return list(dict.fromkeys(list(c['streamers']) + topics.groups(c)['core']))

    def update_index(self):
        self.app.work(lambda: (self.app.job_index(self.channels()), self.app.q.put(('call', lambda: self.load_ideas(True))))[0], 'Indexing clips...')

    def load_ideas(self, force=False):
        def work():
            try:
                rows = topics.suggest(settings())
            except Exception as e:
                rows = []
                self.app.log('Ideas: ' + str(e))
            self.app.q.put(('call', lambda: self.show_ideas(rows)))
        threading.Thread(target=work, daemon=True).start()

    def show_ideas(self, rows):
        self.idea_rows = rows
        self.ideas.delete(*self.ideas.get_children())
        for i, x in enumerate(rows[:120]):
            self.ideas.insert('', 'end', iid=str(i), values=(x['potential'], x['title'], x['moments'], x['minutes'], x['why']))
        if not rows:
            self.info.set('Press "Update clip index" once to see which videos your clips can support.')

    def take(self):
        sel = self.ideas.selection()
        if sel:
            self.idea.set(self.idea_rows[int(sel[0])]['text'])
            self.preview()

    def spec(self):
        t = self.idea.get().strip()
        if not t:
            messagebox.showinfo('Idea', 'Type a video idea or pick one from the list.')
            return None
        sp = topics.parse(t, settings())
        try:
            sp['minutes'] = min(25, max(2, float(self.mins.get())))
        except ValueError:
            sp['minutes'] = 10
        return sp

    def preview(self):
        sp = self.spec()
        if not sp:
            return
        self.info.set('Searching...')

        def work():
            try:
                rows = topics.search(sp, 200)
            except Exception as e:
                rows = []
                self.app.log('Search: ' + str(e))
            self.app.q.put(('call', lambda: self.show(sp, rows)))
        threading.Thread(target=work, daemon=True).start()

    def show(self, sp, rows):
        self.found = rows
        self.cands.delete(*self.cands.get_children())
        good = [m for m in rows if m.get('rel', 1) >= .5]
        for i, m in enumerate(rows[:200]):
            self.cands.insert('', 'end', iid=str(i), values=(m['people'], m['streamer'], time.strftime('%b %d', time.localtime(m['ts'])), '  /  '.join(t[:40] for t in m['titles'][:3]) or '(untitled)', f"{m['views']:,}", f"{m['best']['seconds']:.0f}",
                                                                f"{100 * m.get('rel', 1):.0f}%"))
        topic = topics.TOPICS[sp['topic']]['name'] if sp['topic'] else ', '.join(sp['words']) or 'anything funny'
        self.info.set(f"\"{sp['title']}\":  {', '.join(sp['streamers'])}   |   last {sp['days']} days   |   looking for: {topic}   |   {'told day by day' if sp['kind'] == 'recap' else 'best moments first and last'}   |   "
                      f"{len(good)} clear matches" + (f' + {len(rows) - len(good)} to check by listening' if len(rows) > len(good) else '') + f"   |   aiming for {sp['minutes']:.0f} min")

    def open_web(self):
        sel = self.cands.selection()
        if sel:
            webbrowser.open(self.found[int(sel[0])]['best']['url'])

    def exclude(self):
        for i in self.cands.selection():
            m = self.found[int(i)]
            record_sources([index.to_source(m['best'], m)])
            update_source(m['best']['id'], state='skipped', note='Excluded by you')
        self.preview()

    def build(self):
        sp = self.spec()
        if not sp:
            return
        c = settings()
        save_settings({**c, 'video_minutes': int(sp['minutes'])})
        self.app.work(lambda: (longform.make(sp, settings(), self.app.log, self.app.progress), 'Video ready in Review & approve')[1], f'Building "{sp["title"]}"...')


class VideoEditor(tk.Toplevel):
    """Reorder, drop or trim scenes of a long video, then rebuild (unchanged scenes are reused)."""

    def __init__(self, app, pid):
        super().__init__(app.root, bg=BG)
        self.app, self.pid = app, pid
        self.plan = json.loads(production(pid)['plan_json'])
        self.title('Edit video: ' + self.plan['spec']['title'])
        self.geometry('1120x760')
        ttk.Label(self, text='Scenes play top to bottom. Switch scenes off, move them, or change the text. Unchanged scenes are not re-rendered.', style='Dim.TLabel', padding=12).pack(anchor='w')
        w, self.tree = scrolled(self, [('on', 'Use', 50, 'center'), ('who', 'Streamer', 110, 'w'), ('title', 'On-screen line / chapter', 420, 'w'), ('len', 'Sec', 50, 'center'), ('val', 'Score', 60, 'center'), ('why', 'Why', 280, 'w')], multi=False)
        w.pack(fill='both', expand=True, padx=12)
        self.tree.bind('<Double-1>', lambda _: self.play())
        b = ttk.Frame(self, padding=12)
        b.pack(fill='x')
        for t, fn in (('Use / skip', self.toggle), ('Move up', lambda: self.move(-1)), ('Move down', lambda: self.move(1)), ('Edit text', self.text), ('Trim start/end', self.trim), ('Play source', self.play)):
            ttk.Button(b, text=t, command=fn).pack(side='left', padx=3)
        b2 = ttk.Frame(self, padding=(12, 0, 12, 12))
        b2.pack(fill='x')
        self.hook = tk.BooleanVar(value=bool(self.plan.get('hook')))
        ttk.Checkbutton(b2, text='Open with a short teaser of the best moment', variable=self.hook).pack(side='left')
        self.cap = tk.BooleanVar(value=self.plan.get('hide_captions', True))
        ttk.Checkbutton(b2, text="Crop off Twitch's own captions", variable=self.cap).pack(side='left', padx=10)
        self.low = tk.BooleanVar(value=self.plan.get('lower_third', True))
        ttk.Checkbutton(b2, text='Corner labels', variable=self.low).pack(side='left')
        ttk.Button(b2, text='Voice-over file...', command=self.voice).pack(side='left', padx=10)
        self.vtxt = tk.StringVar(value=', '.join(Path(v['file']).name + f" @{v['at']}s" for v in self.plan.get('voice') or []))
        ttk.Label(b2, textvariable=self.vtxt, style='Dim.TLabel').pack(side='left')
        ttk.Button(b2, text='Rebuild video', style='Go.TButton', command=self.go).pack(side='right')
        self.keep_hook = self.plan.get('hook')
        self.fill()

    def fill(self, sel=None):
        self.tree.delete(*self.tree.get_children())
        for i, x in enumerate(self.plan['segments']):
            self.tree.insert('', 'end', iid=str(i), values=('yes' if x.get('on', True) else 'no', x['streamer'], (x.get('day') + ': ' if x.get('day') else '') + (x['title'] or '(no line)'), f"{sum(p['b'] - p['a'] for p in x['parts']):.0f}", f"{100 * x['value']:.0f}", ', '.join(x['why'][:3])))
        if sel is not None:
            self.tree.selection_set(str(sel))

    def i(self):
        s_ = self.tree.selection()
        return int(s_[0]) if s_ else None

    def toggle(self):
        i = self.i()
        if i is not None:
            self.plan['segments'][i]['on'] = not self.plan['segments'][i].get('on', True)
            self.fill(i)

    def move(self, d):
        i = self.i()
        if i is not None and 0 <= i + d < len(self.plan['segments']):
            q = self.plan['segments']
            q[i], q[i + d] = q[i + d], q[i]
            self.fill(i + d)

    def text(self):
        i = self.i()
        if i is not None:
            v = AskText(self, 'On-screen line and chapter name (empty for none)', self.plan['segments'][i]['title']).value
            if v is not None:
                self.plan['segments'][i]['title'] = v.strip()
                self.fill(i)

    def trim(self):
        i = self.i()
        if i is None:
            return
        pt = self.plan['segments'][i]['parts']
        v = AskText(self, 'Start and end in seconds of the source clip(s), e.g. 3.5 24', f"{pt[0]['a']} {pt[-1]['b']}").value
        try:
            a, z = (float(x) for x in v.replace(',', ' ').split())
            if len(pt) == 1 and z - a < 2:
                raise ValueError
            pt[0]['a'], pt[-1]['b'] = a, z
            self.fill(i)
        except (ValueError, AttributeError):
            if v is not None:
                messagebox.showinfo('Trim', 'Enter two numbers, at least 2 seconds apart.', parent=self)

    def play(self):
        i = self.i()
        if i is not None:
            self.app.open_path(self.plan['segments'][i]['parts'][0]['path'])

    def voice(self):
        f = filedialog.askopenfilename(title='Your voice-over (mp3, wav, m4a)', filetypes=[('Audio', '*.wav *.mp3 *.m4a *.ogg *.flac')], parent=self)
        if not f:
            return
        at = AskText(self, 'Start at which second of the video?', '0.5').value
        try:
            self.plan.setdefault('voice', []).append({'file': f, 'at': float(at)})
            self.vtxt.set(', '.join(Path(v['file']).name + f" @{v['at']}s" for v in self.plan['voice']))
        except (TypeError, ValueError):
            pass

    def go(self):
        self.plan['hook'] = self.keep_hook if self.hook.get() else None
        self.plan['hide_captions'], self.plan['lower_third'] = self.cap.get(), self.low.get()
        pid, plan, app = self.pid, self.plan, self.app
        self.destroy()
        app.work(lambda: (longform.rebuild(pid, settings(), plan, app.log, lambda p_, m='': app.progress(p_, 100, m)), 'Rebuilt. Watch it again before approving.')[1], 'Rebuilding video...')


# ==================================================================== review
class Review:
    def __init__(self, app):
        self.app = app
        self.cur = None
        f = self.frame = ttk.Frame(app.nb, padding=12)
        pane = ttk.Panedwindow(f, orient='horizontal')
        pane.pack(fill='both', expand=True)
        left = ttk.Frame(pane)
        pane.add(left, weight=2)
        self.flt = tk.StringVar(value='Needs your review')
        cb = ttk.Combobox(left, textvariable=self.flt, state='readonly', values=['Needs your review', 'Approved', 'Uploaded (private)', 'Rejected', 'Failed', 'All'])
        cb.pack(fill='x', pady=(0, 6))
        cb.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        wrap, self.tree = scrolled(left, [('kind', 'Type', 66, 'w'), ('title', 'Title', 230, 'w'), ('sec', 'Sec', 44, 'center'), ('risk', 'Safety', 62, 'center'), ('state', 'Status', 110, 'w')], multi=False)
        wrap.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.show())
        self.tree.bind('<Double-1>', lambda e: self.play())
        b = ttk.Frame(left)
        b.pack(fill='x', pady=6)
        app.button(b, 'Upload approved (private)', self.upload, 'Go.TButton', 'Uploads only what you approved, always as PRIVATE. You publish it yourself in YouTube Studio.')
        app.button(b, 'Retry thumbnails', lambda: app.work(lambda: f'{retry_thumbnails(settings(), app.log)} thumbnails attached', lock=False))
        app.button(b, 'Connect YouTube', app.connect_youtube, managed=False)
        mid = ttk.Frame(pane, style='Panel.TFrame', padding=10)
        pane.add(mid, weight=1)
        self.img = tk.Label(mid, bg=PANEL, cursor='hand2')
        self.img.pack()
        self.img.bind('<Button-1>', lambda e: self.play())
        ttk.Button(mid, text='▶  Watch with sound', style='Go.TButton', command=self.play).pack(fill='x', pady=6)
        th = ttk.Frame(mid, style='Panel.TFrame')
        th.pack()
        self.thumb_b = []
        for k in range(3):
            lb = tk.Label(th, bg=PANEL, cursor='hand2', bd=2, relief='flat')
            lb.pack(side='left', padx=3)
            lb.bind('<Button-1>', lambda e, k=k: self.pick_thumb(k))
            self.thumb_b.append(lb)
        ttk.Label(mid, text='Click a thumbnail to choose it', style='PDim.TLabel').pack(pady=2)
        self.facts = tk.StringVar()
        ttk.Label(mid, textvariable=self.facts, style='PDim.TLabel', justify='left', wraplength=330).pack(anchor='w', pady=4)
        outer = ttk.Frame(pane, style='Panel.TFrame')
        pane.add(outer, weight=3)
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        R = ttk.Frame(canvas, style='Panel.TFrame', padding=12)
        win = canvas.create_window(0, 0, window=R, anchor='nw')
        R.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(win, width=e.width))

        def wheel(e):
            if str(e.widget).startswith(str(outer)) and not isinstance(e.widget, tk.Text):
                canvas.yview_scroll(int(-e.delta / 120), 'units')
        app.root.bind_all('<MouseWheel>', wheel, add='+')
        ttk.Label(R, text='What to check', style='H2.TLabel').pack(anchor='w')
        self.flags = text_box(R, height=6)
        self.flags.pack(fill='x', pady=4)
        for t, c in (('hold', RED), ('check', AMBER), ('note', DIM)):
            self.flags.tag_configure(t, foreground=c)
        ttk.Label(R, text='Title', style='H2.TLabel').pack(anchor='w', pady=(6, 0))
        self.title = tk.StringVar()
        self.titlebox = ttk.Combobox(R, textvariable=self.title)
        self.titlebox.pack(fill='x', pady=3)
        ttk.Label(R, text='Description', style='PDim.TLabel').pack(anchor='w')
        self.desc = text_box(R, height=7)
        self.desc.pack(fill='x', pady=3)
        row = ttk.Frame(R, style='Panel.TFrame')
        row.pack(fill='x')
        app.button(row, 'Save text', self.save_text, managed=False)
        app.button(row, 'Copy title', lambda: self.copy(self.title.get()), managed=False)
        app.button(row, 'Copy description', lambda: self.copy(self.desc.get('1.0', 'end').strip()), managed=False)
        app.button(row, 'Open folder', lambda: self.cur and app.open_path(REVIEW / self.cur), managed=False)
        ttk.Label(R, text='Approve', style='H2.TLabel').pack(anchor='w', pady=(10, 2))
        self.ticks = {}
        for c in pr.CHECKLIST:
            t = tk.BooleanVar()
            ttk.Checkbutton(R, text=c, variable=t, style='Panel.TCheckbutton').pack(anchor='w')
            self.ticks[c] = t
        a = ttk.Frame(R, style='Panel.TFrame')
        a.pack(fill='x', pady=8)
        app.button(a, 'Approve for PRIVATE upload', self.approve, 'Go.TButton')
        app.button(a, 'Reject', self.reject, 'Stop.TButton')
        a2 = ttk.Frame(R, style='Panel.TFrame')
        a2.pack(fill='x')
        app.button(a2, 'Retry interrupted upload', self.retry_upload, tip='Only after checking YouTube Studio that the first attempt did not go through.')
        app.button(a2, 'YouTube Studio', lambda: webbrowser.open('https://studio.youtube.com'), managed=False)
        ttk.Label(R, text='Change the edit', style='H2.TLabel').pack(anchor='w', pady=(12, 0))
        g = ttk.Frame(R, style='Panel.TFrame')
        g.pack(fill='x')
        self.headline = tk.StringVar()
        ttk.Label(g, text='On-screen headline', style='PDim.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Entry(g, textvariable=self.headline).grid(row=0, column=1, columnspan=5, sticky='ew', padx=4, pady=2)
        g.columnconfigure(1, weight=1)
        self.itemrow = ttk.Frame(R, style='Panel.TFrame')
        self.itemrow.pack(fill='x', pady=4)
        self.item_vars = []
        o = ttk.Frame(R, style='Panel.TFrame')
        o.pack(fill='x')
        self.opt = {}
        for k, n in (('headline', 'Headline'), ('teaser', 'Cold-open teaser'), ('replay', 'Slow-mo replay'), ('sfx', 'Sound effects'), ('shake', 'Impact shake'), ('censor', 'Auto-censor'), ('blur_chat', 'Blur chat'), ('mute_slurs', 'Mute slurs')):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(o, text=n, variable=v, style='Panel.TCheckbutton').grid(row=len(self.opt) // 4, column=len(self.opt) % 4, sticky='w', padx=(0, 12))
            self.opt[k] = v
        e = ttk.Frame(R, style='Panel.TFrame')
        e.pack(fill='x', pady=3)
        ttk.Label(e, text='Edit energy', style='PDim.TLabel').pack(side='left')
        self.energy = tk.DoubleVar(value=1.0)
        ttk.Scale(e, from_=0, to=1.6, variable=self.energy, length=180).pack(side='left', padx=6)
        ttk.Label(R, text='Commentary (optional, recommended for monetisation: YouTube can treat clips without original commentary as reused content, even with permission)', style='PDim.TLabel', wraplength=440, justify='left').pack(anchor='w', pady=(6, 0))
        v = ttk.Frame(R, style='Panel.TFrame')
        v.pack(fill='x')
        self.voice = text_box(v, height=2)
        self.voice.pack(fill='x', pady=2)
        v2 = ttk.Frame(R, style='Panel.TFrame')
        v2.pack(fill='x')
        ttk.Label(v2, text='Starts at (s)', style='PDim.TLabel').pack(side='left')
        self.voice_at = tk.StringVar(value='0.2')
        ttk.Entry(v2, textvariable=self.voice_at, width=6).pack(side='left', padx=4)
        self.voice_file = tk.StringVar()
        app.button(v2, 'Use my own recording...', self.pick_voice, managed=False)
        ttk.Label(v2, textvariable=self.voice_file, style='PDim.TLabel').pack(side='left', padx=4)
        rr = ttk.Frame(R, style='Panel.TFrame')
        rr.pack(fill='x', pady=6)
        app.button(rr, 'Rebuild with these changes', self.rebuild, tip='Shorts and rankings')
        app.button(rr, 'Edit scenes (long video)', self.edit_video, tip='Reorder, drop, trim or relabel scenes of a YouTube video.')

    def refresh(self):
        keep = self.cur
        self.tree.delete(*self.tree.get_children())
        for p in production_rows():
            name = STATE.get(p['state'], p['state'])
            if self.flt.get() not in ('All', name):
                continue
            self.tree.insert('', 'end', iid=p['id'], tags=(f"risk_{p.get('risk')}",), values=(
                {'ranking': 'Ranking', 'video': 'VIDEO'}.get(p['kind'], 'Single'), p['title'] or p['id'], f"{p.get('seconds') or 0:.0f}", RISK.get(p.get('risk'), RISK[None])[0], name))
        kids = self.tree.get_children()
        if keep in kids:
            self.tree.selection_set(keep)
        elif kids:
            self.tree.selection_set(kids[0])
        else:
            self.cur = None
            self.clear()

    def clear(self):
        self.img.configure(image='')
        for b in self.thumb_b:
            b.configure(image='')
        self.facts.set('Nothing here yet.')
        self.flags.delete('1.0', 'end')
        self.desc.delete('1.0', 'end')
        self.title.set('')

    def show(self):
        sel = self.tree.selection()
        if not sel:
            return
        changed = self.cur != sel[0]
        self.cur = sel[0]
        p = production(self.cur)
        if not p:
            return
        if changed:
            for t in self.ticks.values():
                t.set(False)
        im = self.app.photo(p['thumbnail'], (250, 440))
        self.img.configure(image=im or '')
        self.img.image = im
        opts = json.loads(p.get('options_json') or '{}')
        pick = json.loads(p['plan_json']).get('thumb_pick', 0) if p.get('plan_json') else 0
        for k, lb in enumerate(self.thumb_b):
            f = (opts.get('thumbs') or [])[k:k + 1]
            ph = self.app.photo(f[0], (84, 150)) if f else None
            lb.configure(image=ph or '', bg=GREEN if f and k == pick else PANEL)
            lb.image = ph
        q = json.loads(p.get('qc_json') or '{}')
        m = q.get('metrics', {})
        self.facts.set(f"{ {'ranking': 'Ranking', 'video': 'YouTube video'}.get(p['kind'], 'Single')}   {longform.clock(p.get('seconds') or 0)}   score {p.get('score') or 0:.0f}\n"
                       f"{m.get('fps', '?')} fps   {m.get('lufs', '?')} LUFS   {m.get('mb', '?')} MB\n" + (f"Face on screen {int(100 * m['face_share'])}% of the time\n" if m.get('face_share') is not None else '') +
                       f""
                       f"Status: {STATE.get(p['state'], p['state'])}" + (f"\nYouTube: {p['youtube_id']}" if p.get('youtube_id') else '') + (f"\n{p['error']}" if p.get('error') else ''))
        self.flags.delete('1.0', 'end')
        for fl in json.loads(p.get('flags_json') or '[]'):
            self.flags.insert('end', '• ' + fl + '\n', 'hold' if 'HOLD' in fl else 'check' if 'CHECK' in fl or 'QUALITY' in fl else 'note')
        if changed:
            self.title.set(p['title'] or '')
            self.titlebox['values'] = opts.get('titles') or []
            self.desc.delete('1.0', 'end')
            self.desc.insert('1.0', p['description'] or '')
            self.load_plan(p)

    def load_plan(self, p):
        for w in self.itemrow.winfo_children():
            w.destroy()
        self.item_vars = []
        self.voice.delete('1.0', 'end')
        self.voice_file.set('')
        if not p.get('plan_json'):
            return
        plan = json.loads(p['plan_json'])
        if plan.get('kind') == 'video':
            self.headline.set('')
            ttk.Label(self.itemrow, text=f"{sum(x.get('on', True) for x in plan['segments'])} scenes. Press \"Edit scenes\" to change them.", style='PDim.TLabel').grid(row=0, column=0)
            return
        self.headline.set(plan.get('headline') or '')
        for k, v in self.opt.items():
            v.set(bool(plan['style'].get(k, True)))
        self.energy.set(plan['style'].get('energy', 1.0))
        for v in (plan.get('voice') or [])[:1]:
            self.voice.insert('1.0', v.get('text') or '')
            self.voice_at.set(str(v.get('at', .2)))
            self.voice_file.set(Path(v['file']).name if v.get('file') else '')
            self._vf = v.get('file')
        heads = ['Clip', 'Start (s)', 'End (s)', 'Payoff (s)'] + (['Ranking label'] if p['kind'] == 'ranking' else [])
        for c, h in enumerate(heads):
            ttk.Label(self.itemrow, text=h, style='PDim.TLabel').grid(row=0, column=c, sticky='w', padx=3)
        for r, it in enumerate(plan['items'], 1):
            keep = it['cut']['keep']
            name = f"#{it['rank']}" if it.get('rank') else it['streamer']
            ttk.Label(self.itemrow, text=name, style='Panel.TLabel').grid(row=r, column=0, sticky='w', padx=3)
            vs = {'a': tk.StringVar(value=f'{keep[0][0]:.2f}'), 'b': tk.StringVar(value=f'{keep[-1][1]:.2f}'), 'p': tk.StringVar(value=f"{it['cut']['payoff']:.2f}"), 'l': tk.StringVar(value=it.get('label') or '')}
            vs['orig'] = (vs['a'].get(), vs['b'].get(), vs['p'].get())
            for c, k in enumerate(('a', 'b', 'p'), 1):
                ttk.Entry(self.itemrow, textvariable=vs[k], width=8).grid(row=r, column=c, padx=3, pady=1)
            if p['kind'] == 'ranking':
                ttk.Entry(self.itemrow, textvariable=vs['l'], width=26).grid(row=r, column=4, padx=3)
            self.item_vars.append(vs)

    def play(self):
        p = self.cur and production(self.cur)
        if p and p['video']:
            self.app.open_path(p['video'])

    def pick_thumb(self, k):
        p = self.cur and production(self.cur)
        if p and p['state'] in ('review', 'approved') and k < len(json.loads(p.get('options_json') or '{}').get('thumbs') or []):
            pr.choose_thumb(self.cur, k)
            self.show()

    def copy(self, t):
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(t)
        self.app.say('Copied.')

    def save_text(self):
        if self.cur:
            try:
                pr.set_text(self.cur, self.title.get(), self.desc.get('1.0', 'end'))
                self.app.say('Saved.')
                self.refresh()
            except Exception as e:
                messagebox.showerror('Text', str(e))

    def pick_voice(self):
        f = filedialog.askopenfilename(title='Your commentary recording', filetypes=[('Audio', '*.wav *.mp3 *.m4a *.ogg *.flac'), ('All', '*.*')])
        if f:
            self._vf = f
            self.voice_file.set(Path(f).name)

    def edit_video(self):
        p = self.cur and production(self.cur)
        if p and p['kind'] == 'video' and p.get('plan_json') and p['state'] != 'uploaded_private':
            VideoEditor(self.app, self.cur)
        else:
            messagebox.showinfo('Long videos', 'Select a YouTube video in the list first.')

    def rebuild(self):
        p = self.cur and production(self.cur)
        if not p or not p.get('plan_json') or p['state'] == 'uploaded_private':
            return
        if p['kind'] == 'video':
            VideoEditor(self.app, self.cur)
            return
        if p['state'] == 'approved' and not messagebox.askyesno('Rebuild', 'Rebuilding puts this Short back to "Needs your review". Continue?'):
            return
        plan = json.loads(p['plan_json'])
        items = []
        try:
            for vs in self.item_vars:
                ch = {'label': vs['l'].get().strip()}
                if (vs['a'].get(), vs['b'].get()) != vs['orig'][:2]:
                    a, b = float(vs['a'].get()), float(vs['b'].get())
                    if b - a < 1:
                        raise ValueError('End must be at least 1 second after start.')
                    ch['window'] = [a, b]
                if vs['p'].get() != vs['orig'][2]:
                    ch['payoff'] = float(vs['p'].get())
                items.append(ch)
            at = float(self.voice_at.get() or .2)
        except ValueError as e:
            messagebox.showerror('Check the numbers', str(e))
            return
        st = {**plan['style'], **{k: v.get() for k, v in self.opt.items()}, 'energy': round(self.energy.get(), 2), 'steady': self.energy.get() < .8}
        voice = []
        if self.voice_file.get() and getattr(self, '_vf', None):
            voice = [{'file': self._vf, 'at': at}]
        elif self.voice.get('1.0', 'end').strip():
            voice = [{'text': self.voice.get('1.0', 'end').strip(), 'at': at}]
        ch = {'headline': self.headline.get().strip(), 'style': st, 'items': items, 'voice': voice, 'title': self.title.get().strip(), 'description': self.desc.get('1.0', 'end').strip()}
        pid = self.cur

        def job():
            ok = pr.rebuild(pid, settings(), ch, self.app.log, lambda q, m='': self.app.progress(q, 100, 'Rebuilding: ' + m))
            return 'Rebuilt. Watch it again before approving.' if ok else 'Rebuild failed; the previous version is kept.'
        for t in self.ticks.values():
            t.set(False)
        self.cur = None
        self.app.work(job, 'Rebuilding...')

    def approve(self):
        p = self.cur and production(self.cur)
        if not p or p['state'] != 'review':
            return
        if p.get('risk') == 'hold' and not messagebox.askyesno('High risk', 'This Short contains footage that was held for safety.\n\nUploading it, even privately, can trigger a policy action on a channel that already has two strikes.\n\nApprove anyway?', icon='warning', default='no'):
            return
        try:
            pr.set_text(self.cur, self.title.get(), self.desc.get('1.0', 'end'))
            pr.approve(self.cur, {k: v.get() for k, v in self.ticks.items()})
        except Exception as e:
            messagebox.showinfo('Not approved yet', str(e))
            return
        for t in self.ticks.values():
            t.set(False)
        self.cur = None
        self.app.refresh()

    def reject(self):
        if not self.cur:
            return
        r = messagebox.askyesnocancel('Reject', 'Reject this Short?\n\nYes: reject and put its clips back so they can be used again.\nNo: reject and keep the clips marked as used.')
        if r is None:
            return
        pr.reject(self.cur, release=r)
        self.cur = None
        self.app.refresh()

    def upload(self):
        todo = production_rows('approved')
        if not todo:
            messagebox.showinfo('Nothing approved', 'Watch a Short or video, tick the checklist and approve it first.')
            return
        st = self.app.yt_refresh()
        if not st['ready']:
            if messagebox.askyesno('Connect YouTube first', f'{len(todo)} approved item(s) are ready, but FunnyComm is not connected to your YouTube channel yet.\n\n{st["text"]}\n\nOpen the connection steps now?'):
                self.app.connect_youtube(self.upload)
            return
        name = (st['channel'] or {}).get('title') or 'your channel'
        size = sum(Path(p['video']).stat().st_size for p in todo if Path(p['video'] or '').is_file()) / 1e9
        more = f'\n• ... and {len(todo) - 12} more' if len(todo) > 12 else ''
        if not messagebox.askyesno('Upload privately', f'Upload {len(todo)} approved item(s) ({size:.1f} GB) to "{name}" as PRIVATE?\n\n' + '\n'.join('• ' + (p['title'] or '')[:70] for p in todo[:12]) + more
                                   + '\n\nNobody can see them until you publish in YouTube Studio.'):
            return

        def job():
            out = ytlink.upload_batch(settings(), todo, self.app.log, self.app.progress, media.check_cancel)
            text = ytlink.summary(out, len(todo))
            for line in text.splitlines():
                if line.strip():
                    self.app.log(line)
            show = messagebox.showinfo if len(out['done']) == len(todo) else messagebox.showwarning
            self.app.q.put(('call', lambda: self.app.root.after(300, lambda: show('YouTube upload', text[:1800]))))
            return f'{len(out["done"])} of {len(todo)} uploaded privately'

        def guarded():
            try:
                return job()
            except ytlink.NotConnected as e:
                why = str(e)
                ask = lambda: messagebox.askyesno('YouTube needs attention', why + '\n\nOpen the connection steps now?') and self.app.connect_youtube(self.upload)
                self.app.q.put(('call', lambda: self.app.root.after(300, ask)))
                return 'Nothing uploaded: YouTube is not connected.'
        self.app.work(guarded, 'Connecting to YouTube...')

    def retry_upload(self):
        p = self.cur and production(self.cur)
        if not p or p['state'] != 'approved':
            return
        if messagebox.askyesno('Retry upload', 'Have you checked YouTube Studio and confirmed this Short is NOT already there?'):
            self.app.work(lambda: 'Uploaded privately: ' + upload_private(settings(), p, self.app.log, confirm_retry=True))


# =================================================================== YouTube
class ConnectYouTube(tk.Toplevel):
    """One-time steps that let FunnyComm upload privately to the owner's channel."""

    def __init__(self, app, after=None):
        super().__init__(app.root, bg=BG)
        self.app, self.then = app, after
        self.title('Connect YouTube (one time)')
        self.geometry(f'900x{min(760, self.winfo_screenheight() - 90)}')
        self.minsize(760, 620)
        self.transient(app.root)
        f = ttk.Frame(self, padding=18)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='Connect YouTube', style='H1.TLabel').pack(anchor='w')
        ttk.Label(f, text='You do this once, about 10 minutes. Google makes you create your own free "app" so uploads come from you. '
                          'FunnyComm never sees your password, and the files stay on this PC.', style='Dim.TLabel', wraplength=800, justify='left').pack(anchor='w', pady=(2, 10))
        self.state = tk.StringVar()
        self.lab = ttk.Label(f, textvariable=self.state, font=('Segoe UI', 12, 'bold'), wraplength=800, justify='left')
        self.lab.pack(anchor='w', pady=(0, 8))
        self.btn = {}

        def step(n, title, note, *buttons):
            c = ttk.Frame(f, style='Panel.TFrame', padding=(14, 9))
            c.pack(fill='x', pady=4)
            t = ttk.Frame(c, style='Panel.TFrame')
            t.pack(side='left', fill='x', expand=True)
            ttk.Label(t, text=f'{n}.  {title}', style='H2.TLabel').pack(anchor='w')
            ttk.Label(t, text=note, style='PDim.TLabel', wraplength=520, justify='left').pack(anchor='w')
            for label, fn, st in buttons:
                b = ttk.Button(c, text=label, command=fn, style=st)
                b.pack(side='right', padx=3)
                self.btn[label] = b
        go = lambda k: (lambda: webbrowser.open(ytlink.LINKS[k]))
        step(1, 'Make your sign-in file', 'Use the Google account that owns your channel, with 2-Step Verification on. Application type: Desktop app. Tick the policy, press Continue, then "Download JSON" in the pop-up (Google shows it only once). '
             'Never pay or start a trial; none of this needs a card.', ('2-Step', go('security'), 'TButton'), ('Open', go('personal'), 'TButton'))
        step(2, 'Switch on YouTube', 'On the same account, pick the project "personal-mode-app" at the top of the page, then press the blue Enable button.', ('Open ', go('api'), 'TButton'))
        step(3, 'Give the file to FunnyComm', 'It looks in Downloads, Desktop and Documents. Nobody else should ever get this file, including an AI assistant.',
             ('Choose file...', self.choose, 'TButton'), ('Find it for me', self.find, 'Go.TButton'))
        step(4, 'Sign in', 'Pick the SAME Google account, then your channel, and tick every box. If the page opens in the wrong browser or account, press "Copy sign-in link" and paste it into the Chrome window where that account is signed in. '
             '"Restricted to personal use" or client_user_mismatch means the wrong account.',
             ('Copy sign-in link', self.copy_link, 'TButton'), ('Sign in with Google', self.sign_in, 'Go.TButton'))
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(10, 0))
        ttk.Button(row, text='Test connection', command=self.test).pack(side='left', padx=3)
        ttk.Button(row, text='Use a different channel', command=self.forget).pack(side='left', padx=3)
        ttk.Button(row, text='Close', command=self.destroy).pack(side='right', padx=3)
        ttk.Label(f, text='Google sometimes renames these buttons. Uploads are always private; only you can publish, in YouTube Studio.', style='Dim.TLabel', wraplength=800).pack(anchor='w', pady=(8, 0))
        self.busy = False
        self.show()

    def show(self, text=None, color=None):
        if not self.winfo_exists():
            return
        s = self.app.yt_refresh()
        self.state.set(text or ('✓ ' if s['ready'] else '') + s['text'])
        self.lab.configure(foreground=color or (GREEN if s['ready'] else AMBER))
        self.app.home.refresh()

    def take(self, p):
        try:
            ytlink.import_client(p)
        except (ValueError, OSError) as e:
            self.show(str(e), RED)
            return
        self.show('✓ File accepted. Now press "Sign in with Google".', GREEN)

    def find(self):
        p = ytlink.find_client()
        if not p:
            self.show('No usable sign-in file in Downloads, Desktop or Documents yet. Finish step 1, or press "Choose file...".', RED)
            return
        self.take(p)

    def choose(self):
        p = filedialog.askopenfilename(title='The JSON file you downloaded in step 1', filetypes=[('JSON', '*.json')], parent=self)
        if p:
            self.take(p)

    def background(self, fn, wait):
        if self.busy:
            return
        self.busy = True
        self.show(wait, AMBER)

        def run():
            try:
                ch = fn(settings())
                text, color, ok = f'✓ Connected to {ch["title"]}. You can close this window and press "Upload approved (private)".', GREEN, True
            except Exception as e:
                text, color, ok = str(e)[:400], RED, False

            def done():
                self.busy = False
                self.app.yt_refresh()
                self.show(text, color)
                self.app.log(('Connected to YouTube: ' + text.split('Connected to ')[1].split('. You can')[0]) if ok else 'YouTube: ' + text)
                if ok and self.then and self.winfo_exists():
                    nxt, self.then = self.then, None
                    self.destroy()
                    self.app.root.after(300, nxt)
            self.app.q.put(('call', done))
        threading.Thread(target=run, daemon=True).start()

    def sign_in(self):
        if self.busy:
            link = ytlink.sign_in_link()
            if link:
                webbrowser.open(link)            # pressed again while waiting: show the same page once more
            return
        self.background(ytlink.sign_in, 'Waiting for you to finish the Google sign-in (up to 10 minutes). Wrong account or browser? Press "Copy sign-in link".')

    def copy_link(self):
        link = ytlink.sign_in_link()
        if not link:
            self.show('Press "Sign in with Google" first, then copy the link.', AMBER)
            return
        self.clipboard_clear()
        self.clipboard_append(link)
        self.state.set('Link copied. Paste it into the address bar of the Chrome window where the right account is signed in. Still waiting...')

    def test(self):
        self.background(ytlink.test, 'Asking YouTube which channel is connected...')

    def forget(self):
        if messagebox.askyesno('Different channel', 'Forget the saved sign-in on this PC and sign in again?\n\nNothing on YouTube changes.', parent=self):
            ytlink.disconnect()
            self.show()


# =================================================================== library
class Library:
    def __init__(self, app):
        self.app = app
        f = self.frame = ttk.Frame(app.nb, padding=16)

        def section(title, note):
            c = ttk.Frame(f, style='Panel.TFrame', padding=14)
            c.pack(fill='x', pady=6)
            ttk.Label(c, text=title, style='H2.TLabel').pack(anchor='w')
            ttk.Label(c, text=note, style='PDim.TLabel', wraplength=1100, justify='left').pack(anchor='w', pady=(2, 8))
            r = ttk.Frame(c, style='Panel.TFrame')
            r.pack(fill='x')
            return r
        r = section('Skip what you already used', 'Reads your old clip and finished-Short folders without changing anything. Recognises the same moment even if it was re-encoded, renamed, cropped or edited.')
        app.button(r, 'Scan a folder of old clips', self.scan)
        app.button(r, 'Scan old Shorts Studio folders', self.scan_studio, tip='01_Input_Clips, 02_Finished_Shorts and 03_Used_Originals next to this app.')
        app.button(r, 'Import Twitch links to skip (.txt/.csv)', self.skiplist)
        self.libtxt = tk.StringVar()
        ttk.Label(r, textvariable=self.libtxt, style='PDim.TLabel').pack(side='left', padx=10)
        r = section('Folders', 'Originals, finished Shorts and reports stay in Pipeline_Data. Nothing here is ever deleted by the app.')
        for n, p in (('Downloaded', SRC), ('Review queue', REVIEW), ('Used originals', USED), ('Reports', REPORTS), ('Backups', BACKUPS)):
            app.button(r, n, lambda p=p: app.open_path(p), managed=False)
        r = section('Health', 'Backups are made automatically once a day, here and on the system drive.')
        app.button(r, 'Back up database now', lambda: messagebox.showinfo('Backup', f'Saved {core.backup_db("manual")}'))
        app.button(r, 'Check drive and tools', self.health)
        app.button(r, 'Quality report', lambda: app.open_path(pr.quality_report()))

    def refresh(self):
        with core.db() as con:
            n = con.execute('SELECT COUNT(*) FROM existing_video').fetchone()[0]
            k = con.execute('SELECT COUNT(*) FROM clip_skip').fetchone()[0]
        self.libtxt.set(f'{n} old videos indexed, {k} Twitch clips on the skip list')

    def scan(self):
        d = filedialog.askdirectory(title='Folder with clips you already have (read-only)')
        if d:
            self.app.work(lambda: str(scan_existing_folder(d, self.app.log)), 'Indexing old clips (read-only)...')

    def scan_studio(self):
        base = core.ROOT.parent
        dirs = [base / n for n in ('01_Input_Clips', '02_Finished_Shorts', '03_Used_Originals') if (base / n).is_dir()]

        def job():
            for d in dirs:
                scan_existing_folder(d, self.app.log)
            return f'{len(dirs)} folders indexed'
        self.app.work(job, 'Indexing old Shorts Studio folders (read-only)...')

    def skiplist(self):
        p = filedialog.askopenfilename(title='List of Twitch clip links', filetypes=[('Text or CSV', '*.txt *.csv'), ('All files', '*.*')])
        if p:
            self.app.work(lambda: f'{import_skip_urls(p)} Twitch clips added to the skip list')

    def health(self):
        def job():
            out = [f'Free space: {core.free_gb():.0f} GB', f'Disk warnings in 7 days: {core.drive_warnings(168)}']
            try:
                out.append('FFmpeg: ' + media.tool('ffmpeg'))
            except media.MediaError as e:
                out.append(str(e))
            out.append('Speech model: ' + str(speech.pick_model(settings().get('whisper_model'))[0] or 'not installed'))
            out.append('Nudity scanner: ' + ('ready' if safety.nude_ready() else 'not installed'))
            out.append('Face model: ' + ('ready' if vision.face_model_ready() else 'missing'))
            with core.db() as con:
                out.append('Database: ' + con.execute('PRAGMA integrity_check').fetchone()[0])
            miss = [s['title'] for s in source_rows() if s['state'] in ('downloaded', 'used') and not Path(s['path'] or '').is_file()]
            out.append(f'Missing source files: {len(miss)}')
            for o in out:
                self.app.log(o)
            return '; '.join(out)
        self.app.work(job, lock=False)


# ================================================================== settings
def daily_on():
    r = subprocess.run(['schtasks', '/Query', '/TN', 'FunnyComm_Twitch_Pipeline'], capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) if os.name == 'nt' else None
    return bool(r and r.returncode == 0)


def set_daily(on):
    if os.name != 'nt':
        return
    args = ['/Create', '/SC', 'DAILY', '/ST', '10:00', '/TN', 'FunnyComm_Twitch_Pipeline', '/TR', f'"{core.ROOT / "Run_Daily.bat"}"', '/F'] if on else ['/Delete', '/TN', 'FunnyComm_Twitch_Pipeline', '/F']
    subprocess.run(['schtasks', *args], capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


class ConfigWindow(tk.Toplevel):
    def __init__(self, parent, after_save):
        super().__init__(parent, bg=BG)
        self.parent, self.after_save = parent, after_save
        self.title('Settings')
        self.geometry('900x860')
        self.minsize(760, 640)
        self.conf = settings()
        self.vars, self.toggles = {}, {}
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=14, pady=12)

        def page(name):
            p = ttk.Frame(nb, padding=16)
            nb.add(p, text=name)
            p.columnconfigure(1, weight=1)
            return p

        def entry(p, r, key, label, secret=False):
            ttk.Label(p, text=label).grid(row=r, column=0, sticky='w', pady=4)
            cur = self.conf.get(key, '')
            v = tk.StringVar(value=', '.join(cur) if key == 'streamers' else str(cur))
            self.vars[key] = v
            ttk.Entry(p, textvariable=v, show='*' if secret else '').grid(row=r, column=1, sticky='ew', padx=8, pady=4)

        def check(p, r, key, label):
            v = tk.BooleanVar(value=bool(self.conf.get(key, False)))
            self.toggles[key] = v
            ttk.Checkbutton(p, text=label, variable=v).grid(row=r, column=0, columnspan=2, sticky='w', pady=3)
        m = page('Posting')
        for i, (k, n) in enumerate((('streamers', 'Twitch streamers you have permission for (comma separated)'), ('posts_per_day', 'Shorts per day (YouTube and Google allow up to 100)'), ('post_from', 'First post at'), ('post_until', 'Last post at'))):
            entry(m, i, k, n)
        ttk.Label(m, text='After you approve a Short').grid(row=4, column=0, sticky='w', pady=4)
        self.pmode = tk.StringVar(value='Goes public on its own at a spaced time' if self.conf.get('posting_mode') == 'schedule' else 'Stays private; I publish it')
        ttk.Combobox(m, textvariable=self.pmode, state='readonly', width=44, values=['Goes public on its own at a spaced time', 'Stays private; I publish it']).grid(row=4, column=1, sticky='w', padx=8)
        check(m, 5, 'auto_post', 'Upload approved Shorts automatically while FunnyComm is open')
        check(m, 6, 'show_videos', 'Show the long YouTube video maker (after a restart)')
        self.daily = tk.BooleanVar(value=daily_on())
        ttk.Checkbutton(m, text='Make new Shorts every morning at 10:00 (Windows Task Scheduler)', variable=self.daily).grid(row=7, column=0, columnspan=2, sticky='w', pady=3)
        ttk.Label(m, text='Your own saved clips: put files in "Drop Clips Here\\<streamer>" next to the program; they are used first.', style='Dim.TLabel', wraplength=700).grid(row=8, column=0, columnspan=2, sticky='w', pady=(12, 0))
        a = page('Clips & downloads')
        for i, (k, n) in enumerate((('discover_days', 'Look back (days)'), ('max_clips_per_streamer', 'Clips to keep per streamer'),
                                    ('max_highlights_per_streamer', 'Highlights to list per streamer'), ('max_highlight_seconds', 'Max highlight length (s)'),
                                    ('max_downloads_per_run', 'Downloads per run'), ('max_shorts_per_run', 'Shorts per run'))):
            entry(a, i, k, n)
        for i, (k, n) in enumerate((('permission_confirmed', 'I have permission to download, edit and repost this footage (clipping campaigns)'),
                                    ('permitted_public_clip_downloads', 'Use the public-clip downloader for campaign clips (no Twitch editor role needed)'),
                                    ('allow_owner_highlight_download', 'I own or control the source highlights'), ('twitch_allow_yt_dlp_for_authorized', 'Allow the downloader for permitted highlights')), 8):
            check(a, i, k, n)
        e = page('Editing')
        for i, (k, n) in enumerate((('single_max_seconds', 'Longest single Short (s)'), ('ranked_group_size', 'Clips per ranking (2-5)'), ('ranked_item_seconds', 'Longest ranking clip (s)'),
                                    ('min_score', 'Quality floor (0-100)'), ('edit_energy', 'Edit energy (0 calm - 1.6 wild)'), ('edit_sfx_gain', 'Sound effect level (0-1.5)'),
                                    ('output_fps', 'Shorts frame rate (60, 30 or source)'), ('video_minutes', 'YouTube video length (minutes)'), ('index_days', 'Clip index reach (days)'), ('whisper_model', 'Speech model (auto, small.en, base.en, tiny.en)'))):
            entry(e, i, k, n)
        for i, (k, n) in enumerate((('produce_ranked', 'Make ranked compilations'), ('edit_headline', 'Show a headline for the first seconds (not subtitles)'), ('edit_teaser', 'Cold-open teaser on longer clips'),
                                    ('edit_replay', 'Slow-motion replay on very short clips'), ('edit_sfx', 'Sound effects (original, generated)'), ('edit_censor', 'Censor gameplay during detected nudity'),
                                    ('edit_mute_slurs', 'Mute detected slurs'), ('edit_blur_chat', 'Blur stream chat inside the crop'), ('video_hide_captions', "YouTube videos: crop off Twitch's own captions"), ('video_lower_third', 'YouTube videos: small name labels'), ('export_uncertain_full', 'Conservatively retain full frame when detector is uncertain (legacy studio)')), 12):
            check(e, i, k, n)
        y = page('YouTube & campaigns')
        ttk.Label(y, text='YouTube channel').grid(row=0, column=0, sticky='w', pady=4)
        ttk.Label(y, text='Use the "Connect YouTube" button at the top right of the main window.', style='Dim.TLabel').grid(row=0, column=1, sticky='w', padx=8)
        entry(y, 2, 'youtube_category_id', 'YouTube category (20 gaming, 23 comedy, 24 entertainment)')
        entry(y, 3, 'attribution_line', 'Attribution line in descriptions')
        ttk.Label(y, text='Paid promotion declaration').grid(row=4, column=0, sticky='w', pady=4)
        self.paid = tk.StringVar(value={'yes': 'Yes, paid campaign', 'no': 'No'}.get(self.conf.get('paid_promotion'), 'Decide in YouTube Studio'))
        ttk.Combobox(y, textvariable=self.paid, state='readonly', values=['Decide in YouTube Studio', 'Yes, paid campaign', 'No']).grid(row=4, column=1, sticky='w', padx=8)
        ttk.Label(y, text='If a campaign pays you to post, YouTube and most ad rules expect the paid-promotion box to be ticked. Check your campaign terms.', style='Dim.TLabel', wraplength=640).grid(row=5, column=0, columnspan=2, sticky='w')
        ttk.Label(y, text='Campaign rules, one line per streamer:   login | #hashtags | required line for the description', style='Dim.TLabel').grid(row=6, column=0, columnspan=2, sticky='w', pady=(14, 2))
        self.camp = text_box(y, height=6)
        self.camp.grid(row=7, column=0, columnspan=2, sticky='ew')
        self.camp.insert('1.0', '\n'.join(f"{k} | {v.get('hashtags', '')} | {v.get('note', '')}" for k, v in (self.conf.get('campaigns') or {}).items()))
        t = page('Twitch app')
        entry(t, 0, 'twitch_client_id', 'Client ID')
        entry(t, 1, 'twitch_client_secret', 'Client Secret', True)
        entry(t, 2, 'twitch_editor_id', 'Editor user ID (optional)')
        ttk.Label(t, text='Use credentials from your own developer app and never share them.\nOAuth redirect for the optional editor connection: http://localhost:8765/callback', style='Dim.TLabel').grid(row=3, column=0, columnspan=2, sticky='w', pady=8)
        ttk.Button(t, text='Connect Twitch editor account (optional)', command=self.connect).grid(row=4, column=0, sticky='w')
        row = ttk.Frame(self, padding=(14, 0, 14, 12))
        row.pack(fill='x')
        self.status = tk.StringVar()
        ttk.Label(row, textvariable=self.status, style='Dim.TLabel').pack(side='left')
        ttk.Button(row, text='Close', command=self.destroy).pack(side='right', padx=4)
        ttk.Button(row, text='Save', style='Go.TButton', command=self.save).pack(side='right')

    INT = ('posts_per_day', 'discover_days', 'max_clips_per_streamer', 'max_highlights_per_streamer', 'max_highlight_seconds', 'max_downloads_per_run', 'max_shorts_per_run', 'ranked_group_size', 'single_max_seconds', 'ranked_item_seconds', 'min_score', 'video_minutes', 'index_days')

    def collect(self):
        c = dict(settings())       # keep tokens written by other windows
        for k, v in self.vars.items():
            val = v.get().strip()
            if k == 'streamers':
                val = [z.strip().lstrip('@').lower() for z in val.split(',') if z.strip()]
            elif k in self.INT:
                try:
                    val = max(0 if k in ('min_score', 'max_highlights_per_streamer') else 1, int(float(val)))
                except ValueError:
                    raise ValueError(f'{k.replace("_", " ")} must be a number')
            elif k in ('edit_energy', 'edit_sfx_gain'):
                try:
                    val = min(1.6, max(0.0, float(val)))
                except ValueError:
                    raise ValueError(f'{k.replace("_", " ")} must be a number')
            elif k == 'output_fps':
                val = val if val in ('60', '30', 'source') else '60'
            c[k] = val
        c['posting_mode'] = 'schedule' if self.pmode.get().startswith('Goes public') else 'private'
        if self.daily.get() != daily_on():
            set_daily(self.daily.get())
        for k, v in self.toggles.items():
            c[k] = v.get()
        c['paid_promotion'] = {'Yes, paid campaign': 'yes', 'No': 'no'}.get(self.paid.get(), '')
        camp = {}
        for ln in self.camp.get('1.0', 'end').splitlines():
            parts = [x.strip() for x in ln.split('|')]
            if parts and parts[0]:
                camp[parts[0].lower()] = {'hashtags': parts[1] if len(parts) > 1 else '', 'note': parts[2] if len(parts) > 2 else ''}
        c['campaigns'] = camp
        return c

    def save(self):
        try:
            self.conf = self.collect()
            save_settings(self.conf)
        except Exception as e:
            messagebox.showerror('Check settings', str(e), parent=self)
            return False
        self.status.set('Saved.')
        self.after_save()
        return True

    def connect(self):
        if not self.save():
            return
        if not self.conf.get('twitch_client_id') or not self.conf.get('twitch_client_secret'):
            messagebox.showerror('Missing application', 'Enter Twitch Client ID and Secret first.', parent=self)
            return
        self.status.set('Opening Twitch in your browser...')
        threading.Thread(target=self.auth_twitch, daemon=True).start()

    def auth_twitch(self):
        try:
            app = self.conf
            nonce = secrets.token_urlsafe(24)
            redirect = 'http://localhost:8765/callback'
            saved = {}

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    data = parse_qs(urlparse(self.path).query)
                    if data.get('state', [''])[0] != nonce:
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b'Invalid OAuth state')
                        return
                    saved.update({k: v[0] for k, v in data.items()})
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(b'<h2>Twitch connected. Return to FunnyComm.</h2>')

                def log_message(self, *args):
                    pass
            server = HTTPServer(('127.0.0.1', 8765), Handler)
            server.timeout = 120
            try:
                webbrowser.open('https://id.twitch.tv/oauth2/authorize?' + urlencode({'client_id': app['twitch_client_id'], 'redirect_uri': redirect, 'response_type': 'code',
                                'scope': 'editor:manage:clips channel:manage:clips', 'state': nonce}))
                server.handle_request()
            finally:
                server.server_close()
            if 'code' not in saved:
                raise RuntimeError(saved.get('error_description', 'Authorization not completed within two minutes.'))
            r = requests.post('https://id.twitch.tv/oauth2/token', data={'client_id': app['twitch_client_id'], 'client_secret': app['twitch_client_secret'], 'code': saved['code'],
                              'grant_type': 'authorization_code', 'redirect_uri': redirect}, timeout=30)
            r.raise_for_status()
            data = r.json()
            c = settings()
            c['twitch_user_token'], c['twitch_refresh_token'] = data['access_token'], data.get('refresh_token', '')
            v = requests.get('https://id.twitch.tv/oauth2/validate', headers={'Authorization': 'OAuth ' + data['access_token']}, timeout=25)
            v.raise_for_status()
            c['twitch_editor_id'] = v.json()['user_id']
            save_settings(c)
            self.after(0, lambda: (self.vars['twitch_editor_id'].set(c['twitch_editor_id']), self.status.set('Connected: ' + v.json()['login'])))
        except Exception as e:
            msg = str(e)[:300]
            self.after(0, lambda: self.status.set('Twitch connection: ' + msg))


def main():
    try:
        from ctypes import windll
        windll.shell32.SetCurrentProcessExplicitAppUserModelID('FunnyComm.Studio')
    except Exception:
        pass
    root = tk.Tk()
    try:
        ico = core.ROOT / 'assets' / 'funnycomm.ico'
        if ico.is_file():
            root.iconbitmap(default=str(ico))
    except tk.TclError:
        pass
    try:
        Pipeline(root)
    except Exception as e:
        messagebox.showerror('FunnyComm could not start', f'{e}\n\nRun Self_Test.bat for details.')
        raise
    root.mainloop()


if __name__ == '__main__':
    main()
