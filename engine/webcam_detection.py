"""Local, conservative per-video facecam estimation. No network or downloaded model."""
import math
import numpy as np
import cv2

class DetectionReview(RuntimeError):
    pass

class NoStableFace(DetectionReview):
    pass

def detect(source, meta, ffmpeg, run, allow_estimate=False):
    # Decode independently via FFmpeg, including codecs unavailable in OpenCV.
    times = np.linspace(.03,.97,11)*meta['duration']
    frames=[]
    for t in times:
        try:
            data=run([ffmpeg,'-v','error','-ss',f'{t:.3f}','-i',str(source),'-frames:v','1','-vf','scale=1280:-2','-f','image2pipe','-vcodec','png','-']).stdout
            frame=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
            if frame is not None:frames.append(frame)
        except Exception:continue
    result=detect_frames(frames,allow_estimate)
    from clip_types import green_stats
    green=green_stats(frames,result['face'])
    if green and not result.get('approximate'):
        result.update(green,kind='green')
    elif result.get('approximate'):
        result['kind']='cutout_candidate'
    else:result['kind']='regular'
    return result

def detect_frames(frames,allow_estimate=False):
    if len(frames)<4:raise DetectionReview('Not enough decodable sample frames.')
    h,w=frames[0].shape[:2]; n=len(frames)
    detector=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    if detector.empty():raise RuntimeError('OpenCV face detector missing.')
    alt=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_alt2.xml')
    tracks=[]; edges=[]
    for i,frame in enumerate(frames):
        if frame.shape[:2]!=(h,w):continue
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        edges.append(cv2.Canny(gray,65,150)>0)
        boxes=[]
        # Scan peripheral strips and enlarged corner tiles, with a second cascade
        # for tiny faces and contrast-limited webcams. Keep coordinates in source space.
        regions=[(0,0,int(w*.42),h,1.0),(int(w*.58),0,w,h,1.0),(0,0,w,int(h*.27),1.0),(0,int(h*.73),w,h,1.0)]
        for l in (0,int(w*.58)):
            for t in (0,int(h*.50)):
                regions.append((l,t,l+int(w*.42),min(h,t+int(h*.50)),2.0))
        for l,t,r,b,scale in regions:
            roi=gray[t:b,l:r]; roi=cv2.resize(roi,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC) if scale!=1 else roi
            maxsize=max(24,int(w*.22*scale))
            found=detector.detectMultiScale(roi,scaleFactor=1.08,minNeighbors=4,minSize=(20,20),maxSize=(maxsize,maxsize))
            if scale>1 and not len(found) and not alt.empty():
                found=alt.detectMultiScale(cv2.equalizeHist(roi),scaleFactor=1.08,minNeighbors=4,minSize=(20,20),maxSize=(maxsize,maxsize))
            for xx,yy,ww,hh in found:
                box=(int(l+xx/scale),int(t+yy/scale),int(ww/scale),int(hh/scale))
                bx,by,bw,bh=box
                if not 8<=bw<=w*.22 or not 8<=bh<=h*.40:continue
                if any(math.hypot(bx+bw/2-(ox+ow/2),by+bh/2-(oy+oh/2))<max(bw,ow)*.5 for ox,oy,ow,oh in boxes):continue
                boxes.append(box)
        for x,y,bw,bh in boxes:
            cx,cy=x+bw/2,y+bh/2
            # Streamer overlay usually near the frame perimeter. Ignore central game faces.
            if .27*w<cx<.73*w and .24*h<cy<.76*h:continue
            hit=None;dist=1e9
            for tr in tracks:
                if i in tr['frames']:continue
                mx,my,mw,mh=np.median(tr['boxes'],axis=0); d=math.hypot(cx-(mx+mw/2),cy-(my+mh/2))
                if d<max(18,mw*.95) and .48<bw/mw<2.1 and d<dist:hit=tr;dist=d
            if hit is None:hit={'frames':set(),'boxes':[]};tracks.append(hit)
            hit['frames'].add(i);hit['boxes'].append((int(x),int(y),int(bw),int(bh)))
    required=max(4,math.ceil(n*.46))
    tracks=sorted([t for t in tracks if len(t['frames'])>=required],key=lambda t:len(t['frames']),reverse=True)
    if not tracks:raise NoStableFace('No stable peripheral face detected; webcam absent, hidden, or too small. Use manual crop or gameplay-only.')
    # Persistent edges indicate overlay borders; search all plausible positions around face.
    persist=np.mean(np.stack(edges),axis=0)>=.60
    # Allow tiny compression/edge alignment variation.
    persist=cv2.dilate(persist.astype(np.uint8),np.ones((3,3),np.uint8)).astype(bool)
    def find_border(tr):
        boxes=np.array(tr['boxes']); x,y,bw,bh=np.median(boxes,axis=0);cx,cy=x+bw/2,y+bh/2
        support=len(tr['frames'])/n
        # Boundaries must enclose nearly all observed face boxes.
        lx=max(0,int(np.percentile(boxes[:,0],10)-3));rx=min(w,int(np.percentile(boxes[:,0]+boxes[:,2],90)+3))
        ty=max(0,int(np.percentile(boxes[:,1],10)-3));by=min(h,int(np.percentile(boxes[:,1]+boxes[:,3],90)+3))
        def candidates(axis,lo,hi,center,other_span):
            lim=w if axis==0 else h;lo=max(0,int(lo));hi=min(lim-1,int(hi))
            if lo>hi:return []
            a,b=other_span;a=max(0,int(a));b=min(h if axis==0 else w,int(b));
            if b<=a:return []
            v=persist[a:b,lo:hi+1].mean(axis=0) if axis==0 else persist[lo:hi+1,a:b].mean(axis=1)
            order=np.argsort(-v); result=[]
            for k in order:
                p=lo+int(k)
                if v[k]<.18:break
                if all(abs(p-q)>5 for q in result):result.append(p)
                if len(result)>=7:break
            if lo==0:result.append(0)
            if hi==lim-1:result.append(lim)
            return sorted(set(result))
        L=candidates(0,cx-6.5*bw,lx,cx,(cy-bh,cy+1.4*bh));R=candidates(0,rx,cx+6.5*bw,cx,(cy-bh,cy+1.4*bh))
        T=candidates(1,cy-4.5*bh,ty,cy,(cx-1.4*bw,cx+1.4*bw));B=candidates(1,by,cy+5.5*bh,cy,(cx-1.4*bw,cx+1.4*bw))
        def line(axis,p,a,b):
            lim=w if axis==0 else h
            if p in (0,lim):return .82 # frame edge can also be an overlay boundary
            return float(persist[a:b,p].mean() if axis==0 else persist[p,a:b].mean())
        best=None
        for l in L:
          for r in R:
            rw=r-l
            if not 1.35*bw<=rw<=min(.48*w,11*bw):continue
            for t in T:
              for b in B:
                rh=b-t
                if not 1.25*bh<=rh<=min(.62*h,9*bh):continue
                if not .65<=rw/rh<=2.6:continue
                if not .12<=(cx-l)/rw<=.88 or not .12<=(cy-t)/rh<=.77:continue
                if not (l<.20*w or r>.80*w or t<.17*h or b>.83*h):continue
                scores=[line(0,l,t,b),line(0,r,t,b),line(1,t,l,r),line(1,b,l,r)]
                if min(scores)<.45 or np.mean(scores)<.68:continue
                # Penalize tiny face boxes and favor reasonably centered face.
                score=float(np.mean(scores))-.10*abs((cx-l)/rw-.5)-.09*abs((cy-t)/rh-.43)
                if best is None or score>best[0]:best=(score,l,t,r,b)
        return best, (boxes,x,y,bw,bh,cx,cy,support,lx,rx,ty,by)
    candidates_all=[]
    for tr in tracks:
        best, context=find_border(tr)
        if best:
            score,l,t,r,b=best
            candidates_all.append((score+.12*context[7],best,context))
    if candidates_all:
        candidates_all.sort(key=lambda item:item[0],reverse=True)
        _,best,context=candidates_all[0]
        if len(candidates_all)>1:
            other=candidates_all[1]
            _,l,t,r,b=best; _,ll,tt,rr,bb=other[1]
            overlap=max(0,min(r,rr)-max(l,ll))*max(0,min(b,bb)-max(t,tt))
            union=(r-l)*(b-t)+(rr-ll)*(bb-tt)-overlap
            if overlap/max(1,union)<.55 and candidates_all[0][0]-other[0]<.09:
                raise DetectionReview("Multiple plausible webcam rectangles; select manually.")
        score,l,t,r,b=best
        return {"face":[l/w,t/h,(r-l)/w,(b-t)/h],"method":"stable face + persistent border","support":round(context[7],2),"border_score":round(score,2),"samples":n}
    if len(tracks)>1 and len(tracks[1]["frames"])>=len(tracks[0]["frames"])-2:
        raise DetectionReview("Multiple stable faces without a clear webcam boundary; select manually.")
    _,context=find_border(tracks[0]); boxes,x,y,bw,bh,cx,cy,support,lx,rx,ty,by=context
    if not allow_estimate:raise DetectionReview('Stable face found, but webcam edges are unclear. Enable approximate framing or manually draw the webcam.')
    if not (cx<.24*w or cx>.76*w or cy<.18*h or cy>.82*h) or support<.67:
        raise DetectionReview('Face location or consistency is too uncertain for approximate framing.')
    # Approximate crop around the person, not a claim of exact webcam boundaries.
    rw=min(w*.40,max(bw*2.8,bh*2.4));rh=min(h*.49,rw/1.65)
    l=max(0,min(w-rw,cx-rw/2));t=max(0,min(h-rh,cy-rh*.40))
    if l>lx or l+rw<rx or t>ty or t+rh<by:raise DetectionReview('Approximate crop would cut off the detected face.')
    return {'face':[l/w,t/h,rw/w,rh/h],'method':'approximate face-centered crop (not exact border)','approximate':True,'anchor':[(cx-l)/rw,(cy-t)/rh],'support':round(support,2),'samples':n}
