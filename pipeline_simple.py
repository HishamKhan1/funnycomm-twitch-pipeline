"""The everyday screens: make Shorts, review them, pick clips. Everything else lives under More."""
from __future__ import annotations
import json, threading, time, tkinter as tk, webbrowser
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
import pipeline_core as core
import pipeline_render as pr
import pipeline_sources as sources
import pipeline_youtube as ytlink
from pipeline_core import settings, save_settings, production, production_rows, source_rows
from funnycomm import index, meta

BG, PANEL, CARD, LINE = '#0f141c', '#161d28', '#1d2634', '#2a3646'
FG, DIM, GREEN, AMBER, RED = '#e9eef5', '#93a1b5', '#2be38f', '#ffc247', '#ff5d6c'
MIXES = (('mix', 'Best mix'), ('new', 'Newest'), ('classic', 'All-time'))
LOOKS = (('mix', 'Take turns'), ('square', 'Square'), ('fill', 'Full screen'))


def clock(t):
    lt, now = time.localtime(t), time.localtime()
    day = 'today' if lt[:3] == now[:3] else 'tomorrow' if lt[:3] == time.localtime(time.time() + 86400)[:3] else time.strftime('%a %d %b', lt)
    return f'{day} {time.strftime("%H:%M", lt)}'


def chip(parent, text, on, click):
    """A pill that switches on and off."""
    b = tk.Label(parent, text=text, padx=14, pady=7, cursor='hand2', font=('Segoe UI', 10, 'bold'))

    def paint():
        b.configure(bg=GREEN if on() else CARD, fg='#06110b' if on() else DIM)
    b.bind('<Button-1>', lambda e: (click(), paint()))
    b.paint = paint
    paint()
    b.pack(side='left', padx=4)
    return b


class Home:
    def __init__(self, app):
        self.app = app
        f = self.frame = ttk.Frame(app.nb, padding=(26, 18))
        self.next = tk.StringVar()
        ttk.Label(f, textvariable=self.next, font=('Segoe UI', 13, 'bold'), foreground=GREEN, wraplength=1200, justify='left').pack(anchor='w', pady=(0, 14))
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(0, 6))
        ttk.Label(row, text='Streamers', style='Dim.TLabel', width=11).pack(side='left')
        self.who_row = ttk.Frame(row)
        self.who_row.pack(side='left')
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(0, 14))
        ttk.Label(row, text='Clips from', style='Dim.TLabel', width=11).pack(side='left')
        self.mix_chips = [chip(row, name, lambda k=k: settings().get('clip_mix', 'mix') == k, lambda k=k: self.set_mix(k)) for k, name in MIXES]
        ttk.Label(row, text='   Best mix = new clips, recent ones and all-time classics', style='Dim.TLabel').pack(side='left')
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(0, 14))
        ttk.Label(row, text='Shorts look', style='Dim.TLabel', width=11).pack(side='left')
        self.look_chips = [chip(row, name, lambda k=k: settings().get('short_look', 'mix') == k, lambda k=k: self.set_look(k)) for k, name in LOOKS]
        self.looks = tk.StringVar()
        ttk.Label(row, textvariable=self.looks, style='Dim.TLabel').pack(side='left', padx=(8, 0))
        self.go = tk.Button(f, text='', font=('Segoe UI', 20, 'bold'), bg=GREEN, fg='#06110b', activebackground='#58f0aa', relief='flat', bd=0, pady=16, cursor='hand2', command=self.make)
        self.go.pack(fill='x', pady=(0, 16))
        app.controls.append(self.go)
        cards = ttk.Frame(f)
        cards.pack(fill='x')
        self.cards = {}
        for k, title, label, fn in (('review', 'To review', 'Review now', lambda: app.nb.select(app.review.frame)), ('post', 'Posting', 'Pause', self.toggle_post), ('space', 'Storage', 'Free up space', self.free)):
            c = ttk.Frame(cards, style='Card.TFrame', padding=(18, 14))
            c.pack(side='left', fill='both', expand=True, padx=5)
            ttk.Label(c, text=title, style='CDim.TLabel').pack(anchor='w')
            big, small = tk.StringVar(), tk.StringVar()
            ttk.Label(c, textvariable=big, style='Big.TLabel').pack(anchor='w')
            ttk.Label(c, textvariable=small, style='CDim.TLabel', wraplength=360, justify='left').pack(anchor='w', pady=(0, 8))
            b = ttk.Button(c, text=label, command=fn)
            b.pack(anchor='w', side='bottom')
            self.cards[k] = (big, small, b)
        ttk.Label(f, text='What is happening', style='Dim.TLabel').pack(anchor='w', pady=(16, 2))
        self.box = tk.Text(f, bg=CARD, fg=FG, relief='flat', wrap='word', padx=10, pady=8, font=('Segoe UI', 10), height=8, highlightthickness=1, highlightbackground=LINE)
        self.box.pack(fill='both', expand=True)
        for t, c in (('bad', RED), ('warn', AMBER), ('good', GREEN)):
            self.box.tag_configure(t, foreground=c)
        self.chips = {}
        self.space = None
        self.build_chips()

    def build_chips(self):
        for w in self.who_row.winfo_children():
            w.destroy()
        self.chips = {w: chip(self.who_row, meta.display(w), lambda w=w: w not in (settings().get('streamers_off') or []), lambda w=w: self.flip(w)) for w in settings()['streamers']}

    def active(self):
        c = settings()
        return [w for w in c['streamers'] if w not in (c.get('streamers_off') or [])]

    def flip(self, w):
        c = settings()
        off = set(c.get('streamers_off') or [])
        off ^= {w}
        if len(off) >= len(c['streamers']):
            off.discard(w)
        c['streamers_off'] = sorted(off)
        save_settings(c)
        self.refresh()

    def set_mix(self, k):
        c = settings()
        c['clip_mix'] = k
        save_settings(c)
        for b in self.mix_chips:
            b.paint()

    def set_look(self, k):
        c = settings()
        c['short_look'] = k
        save_settings(c)
        for b in self.look_chips:
            b.paint()

    def look_line(self):
        try:
            r = pr.looks_report()
        except Exception:
            return ''
        part = lambda k, name: f"{name}: {r[k]['made']} made" + (f", {r[k]['public']} public, {r[k]['views']:,} views on average" if r[k]['public'] else '')
        return '   ' + part('square', 'Square') + '   |   ' + part('fill', 'Full screen')

    def add_log(self, m):
        tag = 'bad' if m.startswith(('ERROR', 'FAILED', 'BUILD FAILED', 'UPLOAD ERROR')) else 'warn' if m.startswith(('HELD', 'SKIPPED', 'SAME', 'ALREADY', 'Drive', 'NOTE', 'Not installed')) else 'good' if m.startswith(('READY', 'PRIVATE', 'DONE', 'Connected', 'Freed')) else ''
        self.box.insert('end', time.strftime('%H:%M  ') + m + '\n', tag)
        self.box.see('end')

    def wanted(self, c=None):
        c = c or settings()
        waiting = sum(p['state'] == 'review' and p['kind'] != 'video' for p in production_rows())
        return max(0, int(c.get('posts_per_day') or 20) - waiting), waiting

    def refresh(self):
        c = settings()
        if list(self.chips) != list(c['streamers']):
            self.build_chips()
        for b in self.chips.values():
            b.paint()
        self.looks.set(self.look_line())
        want, waiting = self.wanted(c)
        n = want or 5
        self.go.configure(text=f'Make {n} Shorts')
        self.count = n
        big, small, b = self.cards['review']
        big.set(str(waiting))
        small.set('Shorts ready to watch' if waiting else 'Nothing waiting')
        o = ytlink.outlook(c)
        st = ytlink.status(c)
        big, small, b = self.cards['post']
        mode = c.get('posting_mode') == 'schedule'
        if not st['ready']:
            big.set('Off')
            small.set('Connect YouTube first')
            b.configure(text='Connect YouTube')
        elif o['paused']:
            big.set('Paused')
            small.set(o['paused']['why'][:160])
            b.configure(text='Start again')
        elif not c.get('auto_post', True):
            big.set('Paused')
            small.set('Nothing uploads until you start it again')
            b.configure(text='Start posting')
        else:
            big.set(f'{o["today"]} of {c["posts_per_day"]} today' if mode else 'Private')
            small.set((f'Next {clock(o["next"])}. {o["ahead"]} scheduled.' if o['next'] else 'Approve Shorts and they go out on their own.') if mode else 'Approved Shorts upload as private; you publish them.')
            b.configure(text='Pause')
        big, small, b = self.cards['space']
        if self.space is None:
            big.set('...')
            threading.Thread(target=self.measure, daemon=True).start()
        else:
            big.set(f'{self.space["bytes"] / 1e9:.1f} GB')
            small.set(f'can be freed ({len(self.space["prods"])} uploaded, {len(self.space["clips"])} clips, temporary files). {self.space["free"]:.0f} GB free now.')
        if not c['twitch_client_id']:
            m = 'Open Settings and add your Twitch app.'
        elif not c.get('permission_confirmed'):
            m = 'Open Settings and confirm you have permission to use this footage.'
        elif waiting:
            m = f'{waiting} Shorts are ready. Watch them and press Approve.'
        else:
            m = 'Press the green button. Shorts appear under Review when they are ready.'
        self.next.set(m)

    def measure(self):
        try:
            p = core.free_plan()
            p['free'] = core.free_gb()
            self.space = p
        except Exception as e:
            self.space = {'bytes': 0, 'prods': [], 'clips': [], 'free': 0}
            self.app.log('Storage: ' + str(e)[:100])
        self.app.q.put(('call', self.refresh))

    # ---------------------------------------------------------------- actions
    def make(self):
        who, n, c = self.active(), self.count, settings()
        self.space = None
        self.app.work(lambda: f'{pr.make_batch(c, n, who, c.get("clip_mix", "mix"), self.app.log, self.app.progress)} new Shorts are ready to review.', f'Making {n} Shorts...')

    def toggle_post(self):
        c = settings()
        if not ytlink.status(c)['ready']:
            self.app.connect_youtube()
            return
        if ytlink.paused():
            ytlink.resume()
            c['auto_post'] = True
        else:
            c['auto_post'] = not c.get('auto_post', True)
        save_settings(c)
        self.refresh()

    def free(self):
        s = self.space
        if not s or not s['bytes']:
            messagebox.showinfo('Free up space', 'There is nothing to delete yet.')
            return
        if not messagebox.askyesno('Free up space', f'Delete about {s["bytes"] / 1e9:.1f} GB from this PC?\n\n• {len(s["prods"])} uploaded Shorts and videos (each checked on YouTube first)\n• {len(s["clips"])} Twitch clips used in them\n• Temporary editing files\n\n'
                                   'This cannot be undone. Anything waiting for review, approved or not on YouTube is kept, and so are titles, covers and the history that stops repeats. Nothing on YouTube changes.', icon='warning', default='no'):
            return

        def job():
            r = ytlink.free_space(settings(), self.app.log)
            self.space = None
            return f'Freed {r["bytes"] / 1e9:.1f} GB ({r["files"]} files).' + (f' {r["waiting"]} uploads were kept because YouTube is still processing them.' if r['waiting'] else '')
        self.app.work(job, 'Checking your uploads on YouTube...')


class Review:
    """Watch, approve, next."""

    def __init__(self, app):
        self.app = app
        self.cur, self.watched = None, set()
        f = self.frame = ttk.Frame(app.nb, padding=(20, 14))
        left = ttk.Frame(f)
        left.pack(side='left', fill='y')
        self.count = tk.StringVar()
        ttk.Label(left, textvariable=self.count, style='H2.TLabel').pack(anchor='w', pady=(0, 6))
        self.tree = ttk.Treeview(left, columns=('t',), show='tree', selectmode='browse', height=24)
        self.tree.column('#0', width=300)
        self.tree.pack(fill='y', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.show())
        mid = ttk.Frame(f, padding=(22, 0))
        mid.pack(side='left', fill='y')
        self.img = tk.Label(mid, bg=BG, cursor='hand2')
        self.img.pack()
        self.img.bind('<Button-1>', lambda e: self.play())
        th = ttk.Frame(mid)
        th.pack(pady=8)
        self.tb = []
        for k in range(3):
            lb = tk.Label(th, bg=BG, cursor='hand2', bd=2, relief='flat')
            lb.pack(side='left', padx=4)
            lb.bind('<Button-1>', lambda e, k=k: self.pick(k))
            self.tb.append(lb)
        right = ttk.Frame(f)
        right.pack(side='left', fill='both', expand=True)
        ttk.Label(right, text='Title', style='Dim.TLabel').pack(anchor='w')
        self.title = tk.StringVar()
        self.tbox = ttk.Combobox(right, textvariable=self.title, font=('Segoe UI', 13))
        self.tbox.pack(fill='x', pady=(2, 10), ipady=5)
        self.facts = tk.StringVar()
        ttk.Label(right, textvariable=self.facts, style='Dim.TLabel', wraplength=520, justify='left').pack(anchor='w')
        self.warn = tk.Text(right, bg=BG, fg=AMBER, relief='flat', wrap='word', height=5, font=('Segoe UI', 10), highlightthickness=0)
        self.warn.pack(fill='x', pady=8)
        self.warn.tag_configure('hold', foreground=RED)
        row = ttk.Frame(right)
        row.pack(fill='x', pady=(4, 6))
        mk = lambda text, bg, fg, fn: tk.Button(row, text=text, bg=bg, fg=fg, font=('Segoe UI', 14, 'bold'), relief='flat', bd=0, padx=20, pady=12, cursor='hand2', command=fn)
        self.b_skip = mk('✗  Skip', '#3a2230', '#ff9aa4', self.skip)
        self.b_play = mk('▶  Watch', CARD, FG, self.play)
        self.b_ok = mk('✓  Approve', GREEN, '#06110b', self.approve)
        for b in (self.b_skip, self.b_play, self.b_ok):
            b.pack(side='left', padx=(0, 10))
        self.hint = tk.StringVar()
        ttk.Label(right, textvariable=self.hint, style='Dim.TLabel', wraplength=520, justify='left').pack(anchor='w', pady=(2, 8))
        ttk.Label(right, text='By approving you confirm: you watched it with sound, you may use the footage, no music, film or TV that could be claimed, '
                              'no nudity, slurs, hate or danger, the title and cover are honest, and the edit looks good.', style='Dim.TLabel', wraplength=520, justify='left').pack(anchor='w')
        low = ttk.Frame(right)
        low.pack(anchor='w', pady=14)
        app.button(low, 'Bigger shot', self.wider, tip='Rebuild this Short showing more of the picture', side='left')
        ttk.Button(low, text='Change the edit, description or cover...', command=self.details).pack(side='left', padx=6)
        for key, fn in (('<space>', self.play), ('a', self.approve), ('s', self.skip)):
            self.tree.bind(key, lambda e, fn=fn: (fn(), 'break')[1])

    def rows(self):
        return [p for p in production_rows('review') if p['kind'] != 'video']

    def refresh(self):
        rows = self.rows()
        self.count.set(f'{len(rows)} to review')
        keep = self.cur if any(p['id'] == self.cur for p in rows) else None
        self.tree.delete(*self.tree.get_children())
        for p in rows:
            self.tree.insert('', 'end', iid=p['id'], text=('● ' if p.get('risk') == 'hold' else '') + (p['title'] or '')[:46])
        pick = keep or (rows[0]['id'] if rows else None)
        if pick:
            self.tree.selection_set(pick)
            self.tree.see(pick)
        else:
            self.cur = None
            self.show()

    def show(self):
        sel = self.tree.selection()
        p = production(sel[0]) if sel else None
        self.cur = p['id'] if p else None
        im = self.app.photo(p['thumbnail'], (300, 520)) if p else None
        self.img.configure(image=im or '')
        self.img.image = im
        opts = json.loads((p or {}).get('options_json') or '{}')
        pick = json.loads(p['plan_json']).get('thumb_pick', 0) if p and p.get('plan_json') else 0
        for k, lb in enumerate(self.tb):
            t = (opts.get('thumbs') or [])
            im = self.app.photo(t[k], (78, 138)) if k < len(t) else None
            lb.configure(image=im or '', bg=GREEN if k == pick and im else BG)
            lb.image = im
        self.warn.delete('1.0', 'end')
        if not p:
            self.title.set('')
            self.facts.set('All done. Make more Shorts on the Home screen.')
            self.hint.set('')
        else:
            self.title.set(p['title'] or '')
            self.tbox.configure(values=opts.get('titles') or [])
            src = [core.source(i) or {} for i in json.loads(p['sources_json'])]
            s0 = src[0]
            self.facts.set(' · '.join(x for x in (', '.join(dict.fromkeys(meta.display(s.get('streamer')) for s in src)), f'{p["seconds"] or 0:.0f} s', f'{s0.get("people") or 1} viewers clipped it' if (s0.get('people') or 1) > 1 else '',
                                                  f'{s0.get("views") or 0:,} views', (s0.get('created') or '')[:10], 'score %.0f' % p['score'] if p.get('score') else '') if x))
            for fl in json.loads(p['flags_json'] or '[]'):
                if fl.startswith(('CHECK', 'HOLD', 'QUALITY')):
                    self.warn.insert('end', '• ' + fl + '\n', 'hold' if fl.startswith('HOLD') else '')
            self.hint.set('Watch it once to switch on Approve.  Keys: Space = watch, A = approve, S = skip' if p['id'] not in self.watched else self.plan_text())
        ok = bool(p) and p['id'] in self.watched
        self.b_ok.configure(state='normal' if ok else 'disabled', bg=GREEN if ok else CARD)
        for b in (self.b_skip, self.b_play):
            b.configure(state='normal' if p else 'disabled')

    def plan_text(self):
        c = settings()
        if c.get('posting_mode') == 'schedule':
            t = core.next_slots(c, 1)
            return f'Approve = uploads now as private, goes PUBLIC on its own {clock(t[0])}.' if t else ''
        return 'Approve = uploads as private. You publish it in YouTube Studio.'

    def play(self):
        p = self.cur and production(self.cur)
        if p and p['video']:
            self.app.open_path(p['video'])
            self.watched.add(p['id'])
            self.show()

    def pick(self, k):
        p = self.cur and production(self.cur)
        if p and k < len(json.loads(p.get('options_json') or '{}').get('thumbs') or []):
            pr.choose_thumb(self.cur, k)
            self.show()

    def advance(self):
        ids = list(self.tree.get_children())
        i = ids.index(self.cur) if self.cur in ids else 0
        self.cur = ids[i + 1] if i + 1 < len(ids) else ids[i - 1] if i else None
        self.refresh()
        self.app.home.refresh()
        self.tree.focus_set()

    def approve(self):
        p = self.cur and production(self.cur)
        if not p or p['id'] not in self.watched:
            return
        if p.get('risk') == 'hold' and not messagebox.askyesno('High risk', 'The safety scan held footage in this Short. Approve it anyway?', icon='warning', default='no'):
            return
        try:
            at = pr.quick_approve(p['id'], self.title.get(), settings())
        except Exception as e:
            messagebox.showinfo('Not approved', str(e))
            return
        self.app.log(f'Approved "{p["title"][:44]}"' + (f', public {clock(core.unix(at))}' if at else ''))
        self.advance()

    def skip(self):
        if self.cur:
            pr.reject(self.cur, 'Skipped in review')
            self.advance()

    def wider(self):
        p = self.cur and production(self.cur)
        if not p or not p.get('plan_json'):
            return
        pid, plan = p['id'], json.loads(p['plan_json'])
        items = []
        for it in plan['items']:
            lay = dict(it.get('layout') or {})
            wide = lay.get('mode') == 'wide'
            lay.pop('mode', None)
            items.append({'layout': (lay or None) if wide else {**lay, 'mode': 'wide'}})       # press again to go back
        self.watched.discard(pid)

        def job():
            ok = pr.rebuild(pid, settings(), {'items': items, 'style': {**plan['style'], 'steady': True}}, self.app.log, lambda q, m='': self.app.progress(q, 100, 'Rebuilding: ' + m))
            return 'Rebuilt with a bigger shot. Watch it again.' if ok else 'Rebuild failed; the previous version is kept.'
        self.app.work(job, 'Rebuilding with a bigger shot...')

    def details(self):
        self.app.open_details(self.cur)


class Pick:
    """Browse moments from any place, or paste a link, and turn chosen ones into Shorts."""
    WHEN = (('Last 7 days', 7), ('Last 30 days', 30), ('Last 6 months', 180), ('All time', 0))
    PLACE = (('Twitch clips', 'twitch'), ('Twitch full streams', 'streams'), ('YouTube videos', 'youtube'), ('Kick clips', 'kick'))
    PER_LONG = 8

    def __init__(self, app):
        self.app = app
        self.rows, self.loaded = [], False
        f = self.frame = ttk.Frame(app.nb, padding=(20, 14))
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(0, 8))
        self.place, self.who, self.when, self.word, self.chan = tk.StringVar(value='Twitch clips'), tk.StringVar(), tk.StringVar(value='All time'), tk.StringVar(), tk.StringVar()
        cb0 = ttk.Combobox(row, textvariable=self.place, state='readonly', width=18, values=[w for w, _ in self.PLACE])
        cb0.pack(side='left', padx=(0, 8))
        self.cb = ttk.Combobox(row, textvariable=self.who, state='readonly', width=16)
        self.cb.pack(side='left', padx=(0, 8))
        cb2 = ttk.Combobox(row, textvariable=self.when, state='readonly', width=13, values=[w for w, _ in self.WHEN])
        cb2.pack(side='left', padx=(0, 8))
        self.ch_box = ttk.Frame(row)
        ttk.Label(self.ch_box, text='Channel', style='Dim.TLabel').pack(side='left')
        ce = ttk.Entry(self.ch_box, textvariable=self.chan, width=24)
        ce.pack(side='left', padx=(6, 8))
        ce.bind('<Return>', lambda ev: self.load(True))
        self.w_box = ttk.Frame(row)
        self.w_box.pack(side='left')
        e = ttk.Entry(self.w_box, textvariable=self.word, width=22)
        e.pack(side='left', padx=(0, 8))
        ttk.Label(self.w_box, text='search words', style='Dim.TLabel').pack(side='left')
        cb0.bind('<<ComboboxSelected>>', lambda ev: self.load())
        for w in (self.cb, cb2):
            w.bind('<<ComboboxSelected>>', lambda ev: self.load())
        e.bind('<Return>', lambda ev: self.load())
        app.button(row, 'Make Shorts from selected', self.make, 'Go.TButton', 'Select one or more rows (Ctrl or Shift). Safety checks still apply.', side='right')
        app.button(row, 'Watch', self.watch, managed=False, side='right')

        box = ttk.Frame(f, style='Card.TFrame', padding=(12, 8))
        box.pack(fill='x', pady=(0, 8))
        self.link, self.ok = tk.StringVar(), tk.BooleanVar(value=False)
        ttk.Label(box, text='Or paste a link', style='CDim.TLabel').pack(side='left')
        le = ttk.Entry(box, textvariable=self.link)
        le.pack(side='left', fill='x', expand=True, padx=8)
        le.bind('<Return>', lambda ev: self.add_links())
        ttk.Checkbutton(box, text="I have the owner's permission", variable=self.ok).pack(side='left', padx=(0, 8))
        app.button(box, 'Add link', self.add_links, tip='YouTube, Kick, TikTok, Instagram, X, Reddit, Streamable, Medal, Google Drive, Dropbox, Twitch. Long videos are searched for their best moments.', side='left')
        app.button(box, 'Add files...', self.add_files, tip='Clips or long recordings on this PC', side='left')
        app.button(box, 'Watch a folder...', self.add_folder, tip='New videos in this folder (Google Drive, Dropbox, campaign downloads) are used by themselves', side='left')

        cols = (('title', 'What it is called', 520, 'w'), ('people', 'Clipped by', 90, 'center'), ('views', 'Views', 90, 'e'), ('date', 'Date', 100, 'center'), ('sec', 'Length', 70, 'center'), ('game', 'Category', 170, 'w'))
        wrap = ttk.Frame(f)
        wrap.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show='headings', selectmode='extended')
        for k, t, w, an in cols:
            self.tree.heading(k, text=t, anchor=an)
            self.tree.column(k, width=w, anchor=an, stretch=k == 'title')
        sb = ttk.Scrollbar(wrap, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', lambda e: self.watch())
        self.note = tk.StringVar()
        ttk.Label(f, textvariable=self.note, style='Dim.TLabel', wraplength=1200, justify='left').pack(anchor='w', pady=(6, 0))
        app.nb.bind('<<NotebookTabChanged>>', lambda e: self.maybe(), add='+')

    def maybe(self):
        if str(self.app.nb.select()) == str(self.frame) and not self.loaded:
            self.loaded = True
            self.load()

    def kind(self):
        return dict(self.PLACE)[self.place.get()]

    def people(self):
        c = settings()
        return {**({'CORE (group)': 'core'} if self.kind() not in ('twitch', 'streams') else {}), **{meta.display(w): w for w in c['streamers']}}

    def refresh(self):
        names = list(self.people())
        self.cb.configure(values=names)
        if self.who.get() not in names and names:
            self.who.set(names[0])

    def login(self):
        return self.people().get(self.who.get())

    def load(self, typed=False):
        kind = self.kind()
        self.refresh()
        who, days, word = self.login(), dict(self.WHEN)[self.when.get()], self.word.get().strip().lower()
        if not who:
            return
        outside = kind in ('youtube', 'kick')
        self.ch_box.pack_forget()
        self.w_box.pack_forget()
        if outside:
            c = settings()
            if typed:
                v = self.chan.get().strip().rstrip('/').split('/')[-1] if kind == 'kick' else self.chan.get().strip()
                c.setdefault('places', {}).setdefault(who, {})[kind] = v
                save_settings(c)
            self.chan.set(sources.channel(c, who, kind))
            self.ch_box.pack(side='left')
        self.w_box.pack(side='left')
        name = self.chan.get()
        if outside and not name:
            self.fill([], who, f'Type {self.who.get()}\'s {"YouTube handle, like @name" if kind == "youtube" else "Kick channel name"} next to "Channel" and press Enter.')
            return
        self.note.set('Looking...')

        def run():
            msg = ''
            try:
                if kind == 'twitch':
                    ms = index.unseen([who], time.time() - days * 86400 if days else 0)
                    if word:
                        ms = [m for m in ms if all(any(w in t.lower() for t in m['titles']) for w in word.split())]
                else:
                    ms = sources.list_youtube(settings(), name) if kind == 'youtube' else sources.list_kick(name) if kind == 'kick' else sources.list_streams(settings(), who)
                    ms = [m for m in ms if (not days or m['ts'] > time.time() - days * 86400) and all(w in m['title'].lower() for w in word.split())]
                    used = {s['id'].split(':', 1)[1][:18] for s in source_rows() if str(s['id']).startswith('link:')}
                    import hashlib
                    for m in ms:
                        m['used'] = hashlib.sha256(m['url'].split('&')[0].encode()).hexdigest()[:18] in used
            except Exception as e:
                ms, msg = [], str(e)[:200]
            self.app.q.put(('call', lambda: self.fill(ms, who, msg)))
        threading.Thread(target=run, daemon=True).start()

    def fill(self, ms, who, msg=''):
        kind = self.kind()
        self.rows = ms[:400]
        self.tree.delete(*self.tree.get_children())
        self.tree.heading('people', text='Clipped by' if kind == 'twitch' else 'Used')
        for i, m in enumerate(self.rows):
            day = time.strftime('%Y-%m-%d', time.localtime(m['ts'])) if m['ts'] else ''
            if kind == 'twitch':
                t = next((x for x in m['titles'] if meta.meaningful(x)), m['titles'][0] if m['titles'] else m['best']['title'])
                sec, seen = m['best']['seconds'], m['people']
            else:
                t, sec, seen = m['title'], m['seconds'], '✓' if m.get('used') else '©' if m.get('licensed') else ''
            length = f'{sec:.0f} s' if sec < 120 else f'{sec / 60:.0f} min' if sec < 7200 else f'{sec / 3600:.1f} h'
            self.tree.insert('', 'end', iid=str(i), values=(t[:90], seen, f'{m["views"]:,}', day, length, m['game'][:24]))
        if msg:
            self.note.set(msg)
        elif kind == 'twitch':
            all_ = (index.kv_get('index:' + who) or {}).get('alltime')
            self.note.set(f'{len(ms):,} moments nobody has used yet, best first.' + ('' if all_ else ' Older years are added the first time you make Shorts with "Best mix" or "All-time".'))
        elif kind == 'kick':
            self.note.set(f'{len(ms)} Kick clips, most watched first.')
        else:
            self.note.set(f'{len(ms)} videos. FunnyComm listens through each one, finds where people laugh and shout, and fetches only those seconds (up to {self.PER_LONG} Shorts per video).'
                          + (' © = the owner earns from it through YouTube copyright claims, so expect claims unless their campaign covers you. Listen for music before approving.' if kind == 'youtube' else ' Full streams stay on Twitch for a few weeks.'))

    def chosen(self):
        return [self.rows[int(i)] for i in self.tree.selection() if int(i) < len(self.rows)]

    def watch(self):
        for m in self.chosen()[:1]:
            webbrowser.open(m['best']['url'] if 'best' in m else m['url'])

    def permitted(self, kind, name):
        """Asked once per outside channel."""
        c, key = settings(), f'{kind}:{name}'.lower()
        if sources.allowed(c, key):
            return True
        where = 'YouTube' if kind == 'youtube' else 'Kick'
        if not messagebox.askyesno('Permission', f'Do you have permission from the owner to reuse videos from {name} on {where}?\n\nFor example: you joined their clipping campaign (the CORE, Lacy, Silky and Jason pools on Content Rewards name their footage), or they told you so in writing.\n\n'
                                   f'{where} itself only allows downloads the owner agreed to, and your channel already has strikes. This is asked once per channel.', icon='warning', default='no'):
            return False
        c['places_ok'] = sorted(set(c.get('places_ok') or []) | {key})
        save_settings(c)
        return True

    def make(self):
        ms, c, kind = self.chosen(), settings(), self.kind()
        if not ms:
            messagebox.showinfo('Pick clips', 'Select one or more rows first.')
            return
        if kind == 'twitch':
            who = list(dict.fromkeys(m['streamer'] for m in ms))

            def job():
                n = pr.make_batch(c, len(ms), who, 'mix', self.app.log, self.app.progress, only=ms)
                self.app.q.put(('call', self.load))
                return f'{n} of {len(ms)} became Shorts. They are under Review.'
            self.app.work(job, f'Making {len(ms)} Shorts...')
            return
        if kind in ('youtube', 'kick') and not self.permitted(kind, self.chan.get()):
            return
        self.run([(m['url'], self.login(), m) for m in ms])

    def run(self, jobs, files=()):
        """Links and files -> Shorts."""
        def job():
            c = settings()
            if not c.get('permission_confirmed'):
                raise RuntimeError('Open Settings and confirm you have permission to use this footage first.')
            ids, total, want = [], max(1, len(jobs) + len(files)), 0
            for k, (url, who, hint) in enumerate(jobs):
                try:
                    got = sources.add_link(url, who, c, self.PER_LONG + 4, self.app.log, lambda f, k=k: self.app.progress(.5 * (k + f) / total, 1, f'Getting video {k + 1} of {total}'), hint)
                except sources.SourceError as e:
                    self.app.log('SKIPPED ' + str(e))
                    continue
                want += min(self.PER_LONG, len(got))
                ids += got
            for k, (path, who) in enumerate(files, len(jobs)):
                try:
                    got = sources.add_file(path, who, self.PER_LONG + 4, self.app.log, lambda f, k=k: self.app.progress(.5 * (k + f) / total, 1, f'Reading file {k + 1 - len(jobs)} of {len(files)}'))
                except Exception as e:
                    if type(e).__name__ == 'Cancelled':
                        raise
                    self.app.log(f'SKIPPED {Path(path).name[:40]}: {str(e)[:120]}')
                    continue
                want += min(self.PER_LONG, len(got))
                ids += got
            n = pr.make_from(ids, c, max(1, want), self.app.log, self.app.progress) if ids else 0
            self.app.q.put(('call', self.load))
            return f'{n} new Shorts are ready to review.' if n else 'No new Shorts came out of that. The log says why.'
        self.app.work(job, f'Getting {len(jobs) + len(files)} video(s)...')

    def owner(self):
        return self.login() or (settings()['streamers'] or ['core'])[0]

    def add_links(self):
        links = [u for u in self.link.get().replace(',', ' ').split() if u.startswith('http')]
        if not links:
            messagebox.showinfo('Add a link', 'Paste one or more video links first.')
            return
        if not self.ok.get():
            messagebox.showinfo('Permission', 'Tick "I have the owner\'s permission" first. Only use footage the owner agreed to: your channel already has strikes.')
            return
        self.link.set('')
        self.run([(u, self.owner(), None) for u in links])

    def add_files(self):
        if not self.ok.get():
            messagebox.showinfo('Permission', 'Tick "I have the owner\'s permission" first.')
            return
        fs = filedialog.askopenfilenames(title='Clips or long recordings', filetypes=[('Videos', ' '.join('*' + v for v in sources.VIDEO)), ('All files', '*.*')])
        if fs:
            self.run([], [(f, self.owner()) for f in fs])

    def add_folder(self):
        c = settings()
        now = c.get('watch_folders') or []
        if now and messagebox.askyesno('Watched folders', 'Watching now:\n\n' + '\n'.join(now) + '\n\nStop watching these? (No = add another folder)', default='no'):
            c['watch_folders'] = []
            save_settings(c)
            return
        d = filedialog.askdirectory(title='Folder with footage you may use (Google Drive, Dropbox, campaign downloads)')
        if d:
            c['watch_folders'] = sorted(set(now) | {d})
            save_settings(c)
            messagebox.showinfo('Watched folder', 'New videos in this folder are used the next time you make Shorts. Only put footage there that you may reuse. The files themselves are never changed or deleted.')
