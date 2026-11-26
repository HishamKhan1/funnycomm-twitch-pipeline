import os, sys, json, shutil, subprocess, threading, queue, tempfile, traceback
from pathlib import Path
import tkinter as tk
from webcam_detection import detect, DetectionReview, NoStableFace
from clip_types import isolate, available, track_irl
from smart_reframe import classify, track_expr, sample_frames, analyze_subject
from tkinter import ttk, filedialog, messagebox

ROOT = Path(__file__).resolve().parent
INBOX, OUTPUT, ARCHIVE = [ROOT / n for n in ('01_Input_Clips','02_Finished_Shorts','03_Used_Originals')]
PROFILES = ROOT / 'profiles.json'
REPORTS = ROOT / '04_Detection_Reports'
REPORTS.mkdir(exist_ok=True)
EXTS = {'.mp4','.mov','.mkv','.webm','.avi','.m4v','.ts'}
DEFAULT = {'face':[0.0,0.0,.25,.28], 'game':[0,0,1,1], 'mode':'fill','camera_kind':'regular','face_height':640,'fps':'source'}
CREATE_FLAGS = getattr(subprocess,'CREATE_NO_WINDOW',0)
for d in (INBOX, OUTPUT, ARCHIVE): d.mkdir(exist_ok=True)

def locate(name):
    suffix = '.exe' if os.name == 'nt' else ''
    for p in (ROOT/'ffmpeg'/'bin'/(name+suffix), ROOT/'bin'/(name+suffix), ROOT/(name+suffix)):
        if p.is_file(): return str(p)
    return shutil.which(name)

def run(args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_FLAGS, check=True)

def probe(path, ffprobe):
    data = json.loads(run([ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(path)]).stdout)
    v = next(s for s in data['streams'] if s['codec_type']=='video')
    from fractions import Fraction
    try: fps = float(Fraction(v.get('avg_frame_rate','0/1')))
    except (ValueError,ZeroDivisionError): fps=30
    rotation = next((s.get('rotation',0) for s in v.get('side_data_list',[]) if 'rotation' in s),0)
    w,h=int(v['width']),int(v['height'])
    if abs(rotation)%180==90: w,h=h,w
    return {'w':w,'h':h,'fps':fps or 30,'duration':float(data.get('format',{}).get('duration') or v.get('duration') or 0),'audio':any(s['codec_type']=='audio' for s in data['streams'])}

def pixel_box(rect,w,h):
    x,y,rw,rh=rect
    x=max(0,min(w-2,int(x*w)//2*2));y=max(0,min(h-2,int(y*h)//2*2))
    rw=max(2,min(w-x,int(rw*w)//2*2));rh=max(2,min(h-y,int(rh*h)//2*2))
    return x,y,rw,rh

def crop_box(box):
    x,y,w,h=box
    return f'crop={w}:{h}:{x}:{y}'

def intersect(a,b):
    x=max(a[0],b[0]);y=max(a[1],b[1]);r=min(a[0]+a[2],b[0]+b[2]);d=min(a[1]+a[3],b[1]+b[3])
    return (x,y,r-x,d-y) if r>x and d>y else None

def smart_game_box(game,face,gh):
    # Largest undistorted crop for the lower panel. Prefer center gameplay;
    # shift slightly if that keeps the original webcam out of the crop.
    x,y,w,h=game
    cw=min(w,max(2,int(h*1080/gh)//2*2));ch=min(h,max(2,int(w*gh/1080)//2*2))
    cx=x+(w-cw)//4*2;cy=y+(h-ch)//4*2
    def clamp(v,lo,hi):return max(lo,min(hi,int(v)//2*2))
    fx,fy,fw,fh=face
    xs={cx,clamp(fx+fw+4,x,x+w-cw),clamp(fx-cw-4,x,x+w-cw)}
    ys={cy,clamp(fy+fh+4,y,y+h-ch),clamp(fy-ch-4,y,y+h-ch)}
    choices=[]
    for xx in xs:
        for yy in ys:
            if abs(xx-cx)>.12*w or abs(yy-cy)>.12*h:continue
            box=(xx,yy,cw,ch);over=intersect(box,face)
            area=over[2]*over[3] if over else 0
            choices.append((area,abs(xx-cx)/w+abs(yy-cy)/h,box))
    return min(choices)[2]

def make_filter(profile,w,h,fps):
    fh=int(profile['face_height'])//2*2; gh=1920-fh
    game=pixel_box(profile['game'],w,h)
    if profile.get('camera_kind')=='irl' and not profile.get('game_only'):
        pts=profile.get('track')
        if not pts:raise DetectionReview('IRL tracking needs to be analyzed first.')
        mincw=max(2,min(w,int(h*9/16)//2*2));cw=max(mincw,min(w,int(float(profile.get('track_width',0))*w)//2*2));expr=track_expr(pts,profile.get('scene_cuts',[]),profile.get('preview_seek',0))
        if cw>mincw+2:
            return f"[0:v]crop={cw}:{h}:x='max(0,min(iw-ow,iw*({expr})-ow/2))':y=0,split=2[bg0][fg0];[bg0]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=12,scale=1080:1920,setsar=1[bg];[fg0]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,fps={fps},format=yuv420p[out]"
        return f"[0:v]crop={cw}:{h}:x='max(0,min(iw-ow,iw*({expr})-ow/2))':y=0,scale=1080:1920,setsar=1,fps={fps},format=yuv420p[out]"
    if profile.get('game_only'):
        # No camera: preserve the entire frame. Never invent a camera crop.
        return f'[0:v]{crop_box(game)},split=2[bg0][fg0];[bg0]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=12,scale=1080:1920,setsar=1[bg];[fg0]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,fps={fps},format=yuv420p[out]'
    face=pixel_box(profile['face'],w,h)
    fill=profile.get('mode','fill')=='fill'
    if fill:game=smart_game_box(game,face,gh)
    kind=profile.get('camera_kind','regular')
    if kind=='cutout':
        f=f'[1:v]scale=1080:{fh},setsar=1[face];[0:v]{crop_box(game)},setsar=1[rawgame];'
    elif kind=='green':
        color=profile.get('key_color','0x00ff00')
        f=f'[0:v]split=2[fr][gr];[fr]{crop_box(face)},format=rgba,colorkey={color}:0.30:0.10,scale=1080:{fh}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[keyed];color=c=0x15181c:s=1080x{fh}:r={fps}[back];[back][keyed]overlay=(W-w)/2:H-h:shortest=1[face];[gr]{crop_box(game)},setsar=1[rawgame];'
    else:
        f=f'[0:v]split=2[fr][gr];[fr]{crop_box(face)},scale=1080:{fh}:force_original_aspect_ratio=increase:force_divisible_by=2,crop=1080:{fh},setsar=1[face];[gr]{crop_box(game)},setsar=1[rawgame];' 
    # The webcam is baked into the source. Any remaining overlap is obscured,
    # never represented as restored gameplay. The full-view option can retain it.
    overlap=intersect(game,face) if profile.get('hide_duplicate',True) else None
    if overlap:
        ox,oy,ow,oh=overlap;ox-=game[0];oy-=game[1]
        f+=f'[rawgame]split=2[clean][patch0];[patch0]crop={ow}:{oh}:{ox}:{oy},scale=2:2:flags=area,scale={ow}:{oh}:flags=bilinear,gblur=sigma=12[patch];[clean][patch]overlay={ox}:{oy}:shortest=1[game];'
    else:f+='[rawgame]null[game];'
    if fill:
        f+=f'[game]scale=1080:{gh}:force_original_aspect_ratio=increase:force_divisible_by=2,crop=1080:{gh},setsar=1[g];'
    else:
        # Place complete game directly against facecam, rather than floating
        # in the middle of its section with a large blurred gap above it.
        f+=f'[game]split=2[bg0][fg0];[bg0]scale=270:{gh//4}:force_original_aspect_ratio=increase,crop=270:{gh//4},gblur=sigma=12,scale=1080:{gh},setsar=1[bg];[fg0]scale=1080:{gh}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:0:shortest=1[g];'
    return f+f'[face][g]vstack=inputs=2,fps={fps},format=yuv420p[out]'

def unique(folder, name):
    p=folder/name
    i=2
    while p.exists():
        p=folder/f'{Path(name).stem}_{i}{Path(name).suffix}';i+=1
    return p

def prepare_camera(source, profile, meta, fps, ffmpeg, folder, notify=lambda p:None, seek=0, single=False):
    if profile.get('game_only') or profile.get('camera_kind')!='cutout':return []
    top=Path(folder)/'isolated_camera.mp4'
    isolate(source,pixel_box(profile['face'],meta['w'],meta['h']),int(profile['face_height']),fps,ffmpeg,top,seek,single,notify,meta['duration'],profile.get('anchor'))
    return ['-i',str(top)]

def apply_detection(profile,result,auto_kind='auto'):
    profile.pop('track',None);profile.pop('track_width',None);profile.pop('scene_cuts',None);profile.pop('preview_seek',None)
    profile['game_only']=False
    if result.get('kind')=='full':
        profile['game_only']=True;profile['camera_kind']='regular';return profile
    if result.get('kind')=='irl':
        profile.update(camera_kind='irl',track=result['track'],scene_cuts=result.get('scene_cuts',[]),track_width=result.get('track_width',0));return profile
    profile['face']=result['face'];profile.pop('anchor',None);profile.pop('key_color',None)
    if result.get('anchor'):profile['anchor']=result['anchor']
    if result.get('key_color'):profile['key_color']=result['key_color']
    kind=result.get('kind','regular')
    if auto_kind=='auto':
        if kind=='cutout_candidate':
            if not available():raise DetectionReview('Possible borderless webcam; install Person AI or draw a manual crop.')
            profile['camera_kind']='cutout'
        else:profile['camera_kind']=kind
    else:profile['camera_kind']=auto_kind
    return profile

def encode(source, profile, ffmpeg, ffprobe, notify):
    meta=probe(source,ffprobe)
    if meta['duration']<=0: raise RuntimeError('Could not determine clip duration.')
    if meta['w']<=meta['h']: raise RuntimeError('Input is not horizontal; skipped to avoid incorrect crop.')
    fps = min(60, max(1,round(meta['fps']))) if profile['fps']=='source' else int(profile['fps'])
    final=unique(OUTPUT,source.stem+'_short.mp4')
    temp=final.with_name(final.stem+'.partial.mp4')
    with tempfile.TemporaryDirectory() as camera_temp:
        extra=prepare_camera(source,profile,meta,fps,ffmpeg,camera_temp,lambda p:notify(p*.55))
        return encode_with_camera(source,profile,ffmpeg,ffprobe,notify,meta,fps,final,temp,extra)

def encode_with_camera(source,profile,ffmpeg,ffprobe,notify,meta,fps,final,temp,extra):
    cmd=[ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y','-i',str(source),*extra,'-filter_complex_threads','2','-filter_complex',make_filter(profile,meta['w'],meta['h'],fps),'-map','[out]','-map','0:a:0?','-c:v','libx264','-threads','4','-preset','fast','-crf','19','-c:a','aac','-b:a','192k','-movflags','+faststart','-progress','pipe:1','-nostats',str(temp)]
    try:
        with tempfile.TemporaryFile(mode='w+b') as err:
            proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=err,text=True,creationflags=CREATE_FLAGS)
            for line in proc.stdout:
                if line.startswith('out_time_us='):
                    try: notify((55 if extra else 0)+(0.45 if extra else 1)*min(99, int(int(line.split('=')[1])/1e6/meta['duration']*100)))
                    except ValueError: pass
            if proc.wait()!=0:
                err.seek(0);raise RuntimeError(err.read().decode(errors='replace')[-1800:])
        out=probe(temp,ffprobe)
        if (out['w'],out['h'])!=(1080,1920) or abs(out['duration']-meta['duration'])>max(.5,meta['duration']*.025) or (meta['audio'] and not out['audio']):
            raise RuntimeError('Output validation failed; original retained.')
        temp.rename(final)
    except Exception:
        temp.unlink(missing_ok=True);raise
    return final

class Studio:
    def __init__(self,root):
        self.root=root;root.title('Streamer Shorts Studio v2.4.2');root.geometry('1100x760');root.minsize(900,640)
        self.ffmpeg=locate('ffmpeg');self.ffprobe=locate('ffprobe');self.q=queue.Queue();self.busy=False;self.source=None;self.loaded=False;self.drag=None;self.photo=None;self.previewsize=(1,1);self.offset=(0,0)
        try: self.profiles=json.loads(PROFILES.read_text(encoding='utf-8'))
        except (OSError,ValueError): self.profiles={}
        self.camera_choice=tk.StringVar(value="auto");self.hide_duplicate=tk.BooleanVar(value=True);self.no_face_fallback=tk.BooleanVar(value=False);self.auto=tk.BooleanVar(value=True);self.estimate=tk.BooleanVar(value=False);self.game_only=tk.BooleanVar(value=False);self.manual_crop=False;self.profile=json.loads(json.dumps(DEFAULT));self.name=tk.StringVar(value='Default');self.mode=tk.StringVar(value='fill');self.fps=tk.StringVar(value='source');self.height=tk.StringVar(value='640');self.pick=tk.StringVar(value='face');self.status=tk.StringVar(value='Add clips and convert. Automatic detection checks each clip separately.');self.count=tk.StringVar();self.progress=tk.DoubleVar()
        wrap=ttk.Frame(root,padding=15);wrap.pack(fill='both',expand=True)
        ttk.Label(wrap,text='Streamer Shorts Studio v2.4.2',font=('Segoe UI',20,'bold')).pack(anchor='w')
        ttk.Label(wrap,text='Batch 16:9 → 9:16  •  Mixed clips  •  Smart subject tracking  •  Original audio preserved').pack(anchor='w',pady=(0,10))
        row=ttk.Frame(wrap);row.pack(fill='x');self.buttons=[]
        for title, cmd in [('Add clips',self.add),('Open input',lambda:self.open_dir(INBOX)),('Open Shorts',lambda:self.open_dir(OUTPUT)),('Open used originals',lambda:self.open_dir(ARCHIVE)),('Load sample',self.sample)]:
            b=ttk.Button(row,text=title,command=cmd);b.pack(side='left',padx=(0,5));self.buttons.append(b)
        ttk.Label(wrap,textvariable=self.count).pack(anchor='w',pady=5)
        options=ttk.Frame(wrap);options.pack(fill='x',pady=4)
        ttk.Label(options,text='Streamer profile:').pack(side='left');self.combo=ttk.Combobox(options,textvariable=self.name,values=list(self.profiles),width=18);self.combo.pack(side='left',padx=5);self.combo.bind('<<ComboboxSelected>>',lambda _:self.load_profile());
        for title,cmd in [('Load profile',self.load_profile),('Save profile',self.save_profile)]:
            b=ttk.Button(options,text=title,command=cmd);b.pack(side='left',padx=3);self.buttons.append(b)
        ttk.Label(options,text='  FPS:').pack(side='left');self.fc=ttk.Combobox(options,textvariable=self.fps,values=['source','30','60'],state='readonly',width=7);self.fc.pack(side='left')
        settings=ttk.Frame(wrap);settings.pack(fill='x',pady=5)
        ttk.Radiobutton(settings,text='Draw facecam',variable=self.pick,value='face').pack(side='left');ttk.Radiobutton(settings,text='Draw gameplay region',variable=self.pick,value='game').pack(side='left',padx=8)
        b=ttk.Button(settings,text='Reset gameplay to full frame',command=self.reset_game);b.pack(side='left',padx=4);self.buttons.append(b)
        ttk.Label(settings,text='   Face height:').pack(side='left');self.hc=ttk.Combobox(settings,textvariable=self.height,values=['480','640','800'],state='readonly',width=5);self.hc.pack(side='left')
        auto_row=ttk.Frame(wrap);auto_row.pack(fill='x',pady=5)
        ttk.Checkbutton(auto_row,text='Auto-edit each clip independently',variable=self.auto).pack(side='left')
        ttk.Checkbutton(auto_row,text='Allow approximate face crop',variable=self.estimate).pack(side='left',padx=10)
        for title,cmd in [('Detect sample',self.detect_sample),('Open reports',lambda:self.open_dir(REPORTS))]:
            b=ttk.Button(auto_row,text=title,command=cmd);b.pack(side='left',padx=4);self.buttons.append(b)
        ttk.Checkbutton(auto_row,text='Full view (no webcam split)',variable=self.game_only).pack(side='left',padx=5)
        ttk.Checkbutton(wrap,text='Optional: export uncertain webcam clips in full view (may retain webcam)',variable=self.no_face_fallback).pack(anchor='w')
        types=ttk.Frame(wrap);types.pack(fill='x',pady=3)
        ttk.Label(types,text='Camera type:').pack(side='left')
        ttk.Combobox(types,textvariable=self.camera_choice,values=['auto','regular','green','cutout','irl'],state='readonly',width=12).pack(side='left',padx=5)
        ttk.Label(types,text='Auto: webcam / subject tracking / full view | Person AI: '+('installed' if available() else 'not installed')).pack(side='left')
        actions=ttk.Frame(wrap);actions.pack(fill='x',pady=4)
        self.go=ttk.Button(actions,text='Convert all input clips',command=self.batch);self.go.pack(side='left',fill='x',expand=True,ipady=5,padx=(0,5))
        self.manual_go=ttk.Button(actions,text='Convert selected sample only',command=self.manual_batch);self.manual_go.pack(side='left',fill='x',expand=True,ipady=5);self.buttons.append(self.manual_go)
        self.canvas=tk.Canvas(wrap,bg='#171b24',height=180,highlightthickness=0);self.canvas.pack(fill='both',expand=True,pady=5)
        self.canvas.bind('<ButtonPress-1>',self.start_drag);self.canvas.bind('<B1-Motion>',self.move_drag);self.canvas.bind('<ButtonRelease-1>',self.end_drag);self.canvas.bind('<Configure>',lambda _:self.redraw())
        bottom=ttk.Frame(wrap);bottom.pack(fill='x')
        ttk.Radiobutton(bottom,text='Smart split (cropped game)',variable=self.mode,value='fill').pack(side='left');ttk.Radiobutton(bottom,text='Full gameplay (no crop)',variable=self.mode,value='fit').pack(side='left',padx=8);ttk.Checkbutton(bottom,text='Hide duplicate webcam',variable=self.hide_duplicate).pack(side='left',padx=4)
        b=ttk.Button(bottom,text='Preview edited frame',command=self.edit_preview);b.pack(side='right');self.buttons.append(b)
        self.log=tk.Text(wrap,height=4,wrap='word',state='disabled');self.log.pack(fill='x',pady=5)
        ttk.Progressbar(wrap,variable=self.progress,maximum=100).pack(fill='x');ttk.Label(wrap,textvariable=self.status,wraplength=850).pack(anchor='w',pady=4)
        self.refresh();self.root.after(100,self.poll);self.root.protocol('WM_DELETE_WINDOW',self.close)
        if not self.ffmpeg or not self.ffprobe:self.status.set('FFmpeg / FFprobe not found. Run Install_FFmpeg.ps1 or follow README.')
    def close(self):
        if self.busy: messagebox.showinfo('Conversion active','Wait for the current batch to finish before closing.');return
        self.root.destroy()
    def files(self):return sorted((p for p in INBOX.iterdir() if p.is_file() and p.suffix.lower() in EXTS),key=lambda p:p.name.lower())
    def refresh(self):self.count.set(f'{len(self.files())} input clips  |  {len(list(OUTPUT.glob("*_short*.mp4")))} output files  |  {len([p for p in ARCHIVE.iterdir() if p.is_file()])} archived originals')
    def open_dir(self,p):
        if os.name=='nt':os.startfile(str(p))
        else:subprocess.Popen(['xdg-open',str(p)])
    def add(self):
        files=filedialog.askopenfilenames(title='Add gaming clips',filetypes=[('Video','*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.ts'),('All files','*.*')]);
        for f in files:
            p=Path(f)
            if p.parent.resolve()!=INBOX.resolve():shutil.copy2(p,unique(INBOX,p.name))
        self.refresh()
        if files:self.status.set('Clips added. Auto-detect batch or load a sample for manual review.')
    def check_tools(self):
        if self.ffmpeg and self.ffprobe:return True
        messagebox.showerror('FFmpeg missing','Run Install_FFmpeg.ps1 or install FFmpeg and restart this app.');return False
    def sample(self):
        if not self.check_tools():return
        files=self.files()
        if not files:messagebox.showinfo('No clips','Add video clips to the input folder first.');return
        file=filedialog.askopenfilename(initialdir=INBOX,title='Choose sample from the input folder',filetypes=[('Video','*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.ts')])
        if not file:return
        try:
            self.source=Path(file);m=probe(self.source,self.ffprobe)
            if m['w']<=m['h']:raise ValueError('Choose a horizontal clip.')
            with tempfile.TemporaryDirectory() as d:
                img=Path(d)/'frame.png';run([self.ffmpeg,'-v','error','-ss',str(min(1,m['duration']/2)),'-i',str(self.source),'-frames:v','1','-vf','scale=800:450:force_original_aspect_ratio=decrease','-y',str(img)])
                self.photo=tk.PhotoImage(file=str(img))
            self.loaded=True;self.manual_crop=False;self.profile.pop('anchor',None);self.profile.pop('key_color',None);self.profile.pop('track',None);self.profile.pop('track_width',None);self.profile.pop('scene_cuts',None);self.profile['camera_kind']='regular';self.profile['face']=list(DEFAULT['face']);self.profile['game']=list(DEFAULT['game']);self.game_only.set(False);self.redraw();self.status.set('Sample loaded — not classified yet. Click Detect sample for automatic editing, or draw a facecam manually.')
        except Exception as e:self.source=None;self.loaded=False;messagebox.showerror('Sample error',str(e))
    def redraw(self):
        self.canvas.delete('all');cw=self.canvas.winfo_width();ch=self.canvas.winfo_height()
        if not self.photo:self.canvas.create_text(cw/2,ch/2,text='Load a sample clip to select the facecam',fill='white');return
        factor=max(1, int(max(self.photo.width()/max(1,cw),self.photo.height()/max(1,ch))+.999))
        self.display=self.photo.subsample(factor,factor);w,h=self.display.width(),self.display.height();ox=(cw-w)/2;oy=(ch-h)/2;self.offset=(ox,oy);self.previewsize=(w,h)
        self.canvas.create_image(ox,oy,image=self.display,anchor='nw')
        for key,color in [('game','#38aaff'),('face','#49f28b')]:
            if key=='face' and (self.game_only.get() or self.profile.get('camera_kind')=='irl' or (not self.manual_crop and not (self.drag is not None and self.pick.get()=='face'))):continue
            x,y,rw,rh=self.profile[key];self.canvas.create_rectangle(ox+x*w,oy+y*h,ox+(x+rw)*w,oy+(y+rh)*h,outline=color,width=2)
    def norm(self,e):
        x,y=self.offset;w,h=self.previewsize;return max(0,min(1,(e.x-x)/w)),max(0,min(1,(e.y-y)/h))
    def start_drag(self,e):
        if self.loaded and not self.busy:
            self.drag=self.norm(e)
            if self.pick.get()=='face':
                self.game_only.set(False);self.profile['camera_kind']='regular'
    def move_drag(self,e):
        if self.drag is None:return
        a,b=self.drag;c,d=self.norm(e);self.profile[self.pick.get()]=[min(a,c),min(b,d),abs(a-c),abs(b-d)];self.redraw()
    def end_drag(self,e):
        if self.drag is None:return
        a,b=self.drag;c,d=self.norm(e);self.drag=None
        if abs(a-c)<.02 or abs(b-d)<.02:self.profile[self.pick.get()]=list(DEFAULT[self.pick.get()]);self.status.set('Selection too small; draw again.')
        else:
            if self.pick.get()=='face':
                self.manual_crop=True;self.game_only.set(False);self.profile['camera_kind']='regular';self.profile.pop('track',None);self.profile.pop('track_width',None);self.profile.pop('scene_cuts',None);self.profile.pop('anchor',None);self.profile.pop('key_color',None)
            self.status.set('Crop selected. Preview or save this streamer profile.')
        self.redraw()
    def reset_game(self):self.profile['game']=[0,0,1,1];self.redraw()
    def settings(self):
        p=json.loads(json.dumps(self.profile));p.update(mode=self.mode.get(),fps=self.fps.get(),face_height=int(self.height.get()),game_only=self.game_only.get(),hide_duplicate=self.hide_duplicate.get(),camera_choice=self.camera_choice.get());
        if p['camera_choice']!='auto':p['camera_kind']=p['camera_choice']
        return p
    def load_profile(self):
        if self.name.get() not in self.profiles:messagebox.showinfo('Profiles','No saved profile with this name.');return
        self.profile=json.loads(json.dumps(self.profiles[self.name.get()]));self.manual_crop=True;self.camera_choice.set(self.profile.get('camera_choice','auto'));self.game_only.set(bool(self.profile.get('game_only',False)));self.mode.set(self.profile.get('mode','fill'));self.hide_duplicate.set(self.profile.get('hide_duplicate',True));self.fps.set(self.profile['fps']);self.height.set(str(self.profile['face_height']));self.redraw();self.status.set('Profile loaded. Check it on a sample before batch conversion.')
    def save_profile(self):
        n=self.name.get().strip()
        if not n:return
        self.profiles[n]=self.settings();self.profiles[n].pop('track',None);self.profiles[n].pop('track_width',None);self.profiles[n].pop('scene_cuts',None);PROFILES.write_text(json.dumps(self.profiles,indent=2),encoding='utf-8');self.combo['values']=list(self.profiles);self.status.set(f'Saved profile: {n}')
    def edit_preview(self):
        if not self.check_tools() or not self.source or not self.source.exists():messagebox.showinfo('Preview','Load a sample first.');return
        if not self.game_only.get() and not self.manual_crop and self.camera_choice.get()!='irl':
            messagebox.showinfo('Preview','Detect the webcam or draw its rectangle first. For no webcam, check Full view (no webcam split).');return
        try:
            p=self.settings();m=probe(self.source,self.ffprobe)
            if p.get('camera_kind')=='irl' and not p.get('game_only'):
                if not p.get('track'):
                    frames,rate=sample_frames(self.source,m,self.ffmpeg,run);r=analyze_subject(frames,rate)
                    if r['kind']!='irl':raise DetectionReview('No reliable subject track. Use full view.')
                    p.update(track=r['track'],scene_cuts=r.get('scene_cuts',[]))
                p['preview_seek']=min(1,m['duration']/2)
            with tempfile.TemporaryDirectory() as d:
                img=Path(d)/'preview.png';seek=min(1,m['duration']/2);extra=prepare_camera(self.source,p,m,30,self.ffmpeg,d,seek=seek,single=True);graph=make_filter(p,m['w'],m['h'],30).replace('[out]','[full];[full]scale=324:576[out]');run([self.ffmpeg,'-v','error','-ss',str(seek),'-i',str(self.source),*extra,'-filter_complex_threads','2','-filter_complex',graph,'-map','[out]','-frames:v','1','-y',str(img)])
                photo=tk.PhotoImage(file=str(img))
            w=tk.Toplevel(self.root);w.title('Edited layout preview');label=ttk.Label(w,image=photo);label.image=photo;label.pack(padx=10,pady=10)
        except Exception as e:messagebox.showerror('Preview failed',str(e))
    def append(self,s):self.log.configure(state='normal');self.log.insert('end',s+'\n');self.log.see('end');self.log.configure(state='disabled')
    def detect_sample(self):
        if not self.check_tools() or not self.source or not self.source.exists():
            messagebox.showinfo('Detect sample','Load a sample first.');return
        self.manual_crop=False;self.redraw();self.status.set('Classifying sample and checking subject tracking...');self.root.update_idletasks()
        try:
            m=probe(self.source,self.ffprobe);choice=self.camera_choice.get()
            if choice=='irl':
                frames,rate=sample_frames(self.source,m,self.ffmpeg,run);result=analyze_subject(frames,rate)
                if result['kind']!='irl':raise DetectionReview('No reliable subject track. Use full view.')
            elif choice=='auto':result=classify(self.source,m,self.ffmpeg,run,self.estimate.get(),self.no_face_fallback.get())
            else:result=detect(self.source,m,self.ffmpeg,run,self.estimate.get())
            apply_detection(self.profile,result,choice);self.manual_crop=True;self.game_only.set(bool(self.profile.get('game_only')));self.redraw();self.status.set('Detected: '+result['method']+' / '+result['kind']+'. Preview and adjust if needed.')
        except Exception as e:self.status.set('Review needed: '+str(e));messagebox.showinfo('Review needed',str(e))
    def manual_batch(self):
        if not self.check_tools():return
        if not self.loaded or not self.source or self.source not in self.files():
            messagebox.showinfo('Select input','Load a sample from the input folder first.');return
        if not self.game_only.get() and not self.manual_crop and self.camera_choice.get()!='irl':
            messagebox.showinfo('Crop required','Draw the webcam or click Detect sample first.');return
        if not messagebox.askyesno('Convert sample','Convert this one input clip with the selected settings?'):return
        self.start_batch([self.source],self.settings(),False,False,False)
    def batch(self):
        if not self.check_tools():return
        files=self.files()
        if not files:messagebox.showinfo('No clips','Add clips first.');return
        auto=self.auto.get()
        if not auto and not self.game_only.get() and not self.manual_crop and self.camera_choice.get()!='irl':
            messagebox.showinfo('Select facecam','Load a sample and draw a crop or load a saved profile.');return
        if not messagebox.askyesno('Start batch',f'Convert {len(files)} clips? '+('Each clip will be detected independently. Normal videos use subject tracking or full view. Uncertain webcam clips stay in input unless you enabled the fallback.' if auto else 'All clips use the same manual layout. Only proceed if this is appropriate for all clips.')):return
        p=self.settings()
        if auto:p.update(game=[0,0,1,1],game_only=False);p.pop('track',None);p.pop('track_width',None);p.pop('scene_cuts',None)
        self.start_batch(files,p,auto,self.estimate.get(),self.no_face_fallback.get())
    def start_batch(self,files,p,auto,estimate,fallback):
        self.busy=True;self.go.configure(state='disabled')
        for b in self.buttons:b.configure(state='disabled')
        self.progress.set(0);threading.Thread(target=self.worker,args=(files,p,auto,estimate,fallback),daemon=True).start()
    def worker(self,files,p,auto,estimate,fallback):
        good=bad=review=0
        for i,s in enumerate(files):
            self.q.put(('status',f'Processing {i+1}/{len(files)}: {s.name}'))
            report={'source':s.name,'status':'pending'};final=None
            try:
                cp=dict(p)
                choice=cp.get('camera_choice','auto');m=probe(s,self.ffprobe)
                if choice=='irl' and not cp.get('game_only'):
                    frames,rate=sample_frames(s,m,self.ffmpeg,run);result=analyze_subject(frames,rate)
                    if result['kind']!='irl':raise DetectionReview('No reliable subject track. Use full view.')
                    apply_detection(cp,result,choice);report['detection']=result
                elif auto:
                    self.q.put(('status',f'Classifying {i+1}/{len(files)}: {s.name}'))
                    if choice=='auto':result=classify(s,m,self.ffmpeg,run,estimate,fallback)
                    else:result=detect(s,m,self.ffmpeg,run,estimate)
                    apply_detection(cp,result,choice);report['detection']=result
                    self.q.put(('log',f'DETECTED: {s.name} — {result["method"]} / {result["kind"]}'))
                if cp.get('camera_kind')=='irl' and not cp.get('game_only') and not cp.get('track'):
                    frames,rate=sample_frames(s,m,self.ffmpeg,run);result=analyze_subject(frames,rate)
                    if result['kind']!='irl':raise DetectionReview('No reliable subject track. Use full view.')
                    apply_detection(cp,result,choice);report['detection']=result
                report['settings']=cp
                self.q.put(('status',f'Rendering {i+1}/{len(files)}: {s.name}'))
                final=encode(s,cp,self.ffmpeg,self.ffprobe,lambda pct:self.q.put(('progress',(i+pct/100)*100/len(files))))
                try: archived=Path(shutil.move(str(s),str(unique(ARCHIVE,s.name))))
                except Exception as e:
                    final.unlink(missing_ok=True);raise RuntimeError(f'Could not archive original; output rolled back: {e}')
                good+=1;report.update(status='converted',output=final.name,archived=archived.name);self.q.put(('log',f'OK: {final.name}'))
            except DetectionReview as e:
                review+=1;report.update(status='review',reason=str(e));self.q.put(('log',f'REVIEW: {s.name}: {e}'))
            except Exception as e:
                bad+=1;report.update(status='failed',reason=str(e));self.q.put(('log',f'FAILED: {s.name}: {e}'))
            try:unique(REPORTS,s.stem+'_detection.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            except OSError as e:self.q.put(('log',f'Could not write report: {e}'))
            self.q.put(('progress',(i+1)*100/len(files)))
        self.q.put(('done',(good,bad,review)))
    def poll(self):
        while True:
            try: key,val=self.q.get_nowait()
            except queue.Empty:break
            if key=='status':self.status.set(val)
            elif key=='progress':self.progress.set(val)
            elif key=='log':self.append(val)
            elif key=='done':
                self.busy=False;self.go.configure(state='normal')
                for b in self.buttons:b.configure(state='normal')
                self.loaded=False;self.source=None;self.photo=None;self.redraw();self.refresh();self.status.set(f'Finished: {val[0]} converted, {val[2]} need review, {val[1]} failed. Review/failed originals remain in input.');self.manual_crop=False
        self.root.after(100,self.poll)

if __name__=='__main__':
    root=tk.Tk();Studio(root);root.mainloop()
