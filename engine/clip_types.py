"""Per-clip chroma analysis and optional local MediaPipe person isolation."""
from pathlib import Path
import subprocess, tempfile, math
import cv2
import numpy as np
from webcam_detection import DetectionReview
MODEL=Path(__file__).resolve().parent/'models'/'selfie_segmenter_landscape.tflite'
FLAGS=getattr(subprocess,'CREATE_NO_WINDOW',0)

def green_stats(frames,rect):
    """Require a sizeable, persistent saturated green region in the webcam crop."""
    colors=[]; ratios=[]
    for frame in frames:
        h,w=frame.shape[:2];x,y,a,b=rect
        roi=frame[int(y*h):int((y+b)*h),int(x*w):int((x+a)*w)]
        if not roi.size:continue
        hsv=cv2.cvtColor(roi,cv2.COLOR_BGR2HSV)
        mask=(hsv[:,:,0]>=37)&(hsv[:,:,0]<=89)&(hsv[:,:,1]>=105)&(hsv[:,:,2]>=65)
        ratios.append(float(mask.mean()))
        if mask.any():colors.append(np.median(roi[mask],axis=0))
    if not ratios or sum(v>=.20 for v in ratios)<math.ceil(len(ratios)*.75):return None
    b,g,r=np.median(colors,axis=0).astype(int)
    if g-max(b,r)<35:return None
    return {'key_color':f'0x{r:02x}{g:02x}{b:02x}','green_fraction':round(float(np.median(ratios)),3)}

def available():
    if not MODEL.is_file():return False
    try:
        import mediapipe as mp
        return hasattr(mp,'tasks')
    except Exception:return False

def isolate(source, box, face_height, fps, ffmpeg, target, seek=0, single=False, notify=lambda p:None, duration=0, anchor=None):
    """Decode a CFR webcam crop, segment each frame, and build a dark-backed face panel.
    Never attempts to recover the gameplay under an already composited person.
    """
    if not available():raise DetectionReview('Person isolation not installed. Run Install_Person_AI.bat or use a regular manual crop.')
    import mediapipe as mp
    x,y,w,h=box
    # Bound the working resolution; aspect ratio is preserved.
    scale=min(1,640/max(w,h)); a=max(2,int(w*scale)//2*2); b=max(2,int(h*scale)//2*2)
    args=[ffmpeg,'-v','error','-nostdin','-ss',str(seek),'-i',str(source),'-an','-vf',f'crop={w}:{h}:{x}:{y},scale={a}:{b},fps={fps}', '-pix_fmt','bgr24']
    if single:args+=['-frames:v','1']
    args+=['-f','rawvideo','pipe:1']
    dest=[ffmpeg,'-v','error','-nostdin','-y','-f','rawvideo','-pix_fmt','bgr24','-s',f'540x{face_height//2}','-r',str(fps),'-i','pipe:0','-an','-c:v','libx264','-preset','ultrafast','-crf','12','-pix_fmt','yuv420p',str(target)]
    options=mp.tasks.vision.ImageSegmenterOptions(base_options=mp.tasks.BaseOptions(model_asset_path=str(MODEL)),running_mode=mp.tasks.vision.RunningMode.VIDEO,output_category_mask=False,output_confidence_masks=True)
    n=bad=0; prev=None; missing_run=0; max_missing=0
    with tempfile.TemporaryFile() as de, tempfile.TemporaryFile() as ee, mp.tasks.vision.ImageSegmenter.create_from_options(options) as model:
        dec=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=de,creationflags=FLAGS)
        enc=subprocess.Popen(dest,stdin=subprocess.PIPE,stderr=ee,creationflags=FLAGS)
        try:
            while True:
                buf=dec.stdout.read(a*b*3)
                if not buf:break
                if len(buf)!=a*b*3:raise RuntimeError('Incomplete decoded frame.')
                im=np.frombuffer(buf,np.uint8).reshape(b,a,3)
                rgb=cv2.cvtColor(im,cv2.COLOR_BGR2RGB)
                result=model.segment_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,data=rgb),round(n*1000/fps))
                if not result.confidence_masks or len(result.confidence_masks)<2:raise RuntimeError('Person model returned no foreground mask.')
                m=result.confidence_masks[1].numpy_view().copy();m=cv2.resize(m,(a,b));m=np.clip((m-.22)/.60,0,1)
                # Prefer foreground component connected to the estimated face anchor.
                count,labels,stats,centers=cv2.connectedComponentsWithStats((m>.48).astype(np.uint8),8)
                points=(anchor[0]*a,anchor[1]*b) if anchor else (a*.5,b*.35)
                valid=[i for i in range(1,count) if stats[i,cv2.CC_STAT_AREA]>.012*a*b]
                if valid:
                    k=min(valid,key=lambda i:((centers[i,0]-points[0])/a)**2+((centers[i,1]-points[1])/b)**2)
                    component=cv2.dilate((labels==k).astype(np.uint8),np.ones((7,7),np.uint8));m*=component
                coverage=float((m>.5).mean());
                if coverage<.035 or coverage>.94:
                    bad+=1;missing_run+=1;max_missing=max(max_missing,missing_run)
                else:missing_run=0
                if prev is not None:m=.78*m+.22*prev
                prev=m.copy()
                out_h=face_height//2; fit=min(540/a,out_h/b); ww=max(1,round(a*fit));hh=max(1,round(b*fit));
                fg=cv2.resize(im,(ww,hh),interpolation=cv2.INTER_AREA if fit<1 else cv2.INTER_LINEAR);alpha=cv2.resize(m,(ww,hh))[:,:,None]
                canvas=np.full((out_h,540,3),(28,24,21),dtype=np.uint8); xx=(540-ww)//2;yy=out_h-hh
                canvas[yy:yy+hh,xx:xx+ww]=np.clip(fg*alpha+canvas[yy:yy+hh,xx:xx+ww]*(1-alpha),0,255).astype(np.uint8)
                enc.stdin.write(canvas.tobytes());n+=1
                if n%15==0 and duration:notify(min(99,n/fps/duration*100))
            dec.stdout.close();enc.stdin.close()
            dr=dec.wait();er=enc.wait()
            if dr or er:
                de.seek(0);ee.seek(0);raise RuntimeError((de.read()+ee.read()).decode(errors='replace')[-1000:])
            if not n or bad/n>.12 or max_missing>max(1,round(fps*.4)):
                raise DetectionReview(f'Person masking uncertain ({bad}/{n} poor frames). Use manual regular crop or review.')
        finally:
            for p in (dec,enc):
                if p.poll() is None:p.kill();p.wait()
    return {'frames':n,'weak_masks':bad}

def track_irl(source,meta,ffmpeg,run):
    """Explicit IRL mode only. Track one dominant real-world subject; reject ambiguity."""
    detector=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    times=np.linspace(0,max(0,meta['duration']-.12),max(5,min(61,math.ceil(meta['duration']*2)+1)))
    hits=[]; last=None; missed=0
    for t in times:
        try:
            data=run([ffmpeg,'-v','error','-ss',f'{t:.3f}','-i',str(source),'-frames:v','1','-vf','scale=800:-2','-f','image2pipe','-vcodec','png','-']).stdout
            im=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_GRAYSCALE)
            if im is None:raise ValueError('no image')
        except Exception:missed+=1;continue
        h,w=im.shape; boxes=detector.detectMultiScale(im,1.08,5,minSize=(max(24,int(w*.045)),)*2)
        boxes=sorted(boxes,key=lambda b:b[2]*b[3],reverse=True)
        if not len(boxes):missed+=1;continue
        if len(boxes)>1 and boxes[1][2]*boxes[1][3]>.55*boxes[0][2]*boxes[0][3]:raise DetectionReview('IRL tracking: multiple similarly prominent faces. Review manually.')
        x,y,a,b=boxes[0];cx=(x+a/2)/w
        if last is not None and abs(cx-last)>.36:raise DetectionReview('IRL tracking: sudden subject change. Review manually.')
        last=cx;hits.append((float(t),float(cx)))
    if missed>len(times)*.25 or len(hits)<4:raise DetectionReview('IRL tracking: no consistently visible face.')
    cs=np.array([x[1] for x in hits]);smooth=np.convolve(np.pad(cs,(1,1),mode='edge'),[.18,.64,.18],mode='valid')
    return [[round(t,3),round(float(c),5)] for (t,_),c in zip(hits,smooth)]
