"""Offline scene-aware portrait reframing and conservative mixed-clip routing.
No speech recognition, no hallucinated gameplay, no network calls.
"""
import math
import cv2
import numpy as np
from webcam_detection import detect, DetectionReview, NoStableFace


def sample_frames(source, meta, ffmpeg, run):
    # One decode pass, at 2–4 samples/sec; bound memory and filter complexity.
    rate=min(4., 120/max(meta['duration'],.1))
    w=640; h=max(2,round(w*meta['h']/meta['w']/2)*2)
    data=run([ffmpeg,'-v','error','-nostdin','-i',str(source),'-an','-vf',f'fps={rate:.8f},scale={w}:{h},setsar=1','-pix_fmt','bgr24','-f','rawvideo','-threads','1','pipe:1']).stdout
    size=w*h*3
    if len(data)%size or len(data)<size:raise DetectionReview('Cannot decode enough frames for smart reframing.')
    return np.frombuffer(data,np.uint8).reshape(-1,h,w,3),rate


def face_boxes(frames):
    detector=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    alt=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_alt2.xml')
    if detector.empty():raise RuntimeError('OpenCV face detector missing.')
    out=[]
    for im in frames:
        g=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY); h,w=g.shape
        boxes=detector.detectMultiScale(g,1.09,5,minSize=(max(24,int(w*.035)),)*2)
        if not len(boxes) and not alt.empty():boxes=alt.detectMultiScale(cv2.equalizeHist(g),1.10,5,minSize=(max(24,int(w*.035)),)*2)
        out.append([(x/w,y/h,a/w,b/h) for x,y,a,b in boxes])
    return out


def shot_boundaries(frames):
    cuts=[0]; last=None; hist0=None
    for i,im in enumerate(frames):
        small=cv2.resize(im,(96,54)); hsv=cv2.cvtColor(small,cv2.COLOR_BGR2HSV)
        hist=cv2.calcHist([hsv],[0,1],None,[24,16],[0,180,0,256]); cv2.normalize(hist,hist,alpha=1,norm_type=cv2.NORM_L1)
        if last is not None:
            delta=float(np.abs(small.astype('float32')-last).mean()/255)
            hd=cv2.compareHist(hist0,hist,cv2.HISTCMP_BHATTACHARYYA)
            if (delta>.22 and hd>.42) or delta>.40:cuts.append(i)
        last=small.astype('float32');hist0=hist
    return cuts


def analyze_frames(frames,rate,boxes=None):
    """Plan subject crops if supported; otherwise preserve complete source.
    Multi-person framing keeps nearby faces together. Widely separated people
    cause a full-view fallback instead of falsely guessing the active speaker.
    """
    n=len(frames);h,w=frames[0].shape[:2];crop_width=(h*9/16)/w
    boxes=face_boxes(frames) if boxes is None else boxes
    cuts=shot_boundaries(frames); points=[];needs_full=False;possible_webcam=False;counts={'single':0,'group':0,'wide':0,'unseen':0,'small_edge':0};shot_info=[]
    for lo,hi in zip(cuts,cuts[1:]+[n]):
        centers=[];ids=[];wide=0;small_edge=0
        for i in range(lo,hi):
            bs=sorted(boxes[i],key=lambda b:b[2]*b[3],reverse=True)
            # Tiny faces near the edge might be a missed webcam. Preserve them,
            # do not assume they are a full-screen real-world subject.
            if bs and bs[0][2]<.105 and (bs[0][0]+bs[0][2]/2<.25 or bs[0][0]+bs[0][2]/2>.75):
                small_edge+=1;counts['small_edge']+=1;continue
            if not bs:counts['unseen']+=1;continue
            if bs[0][2]<.055:counts['unseen']+=1;continue
            major=[b for b in bs if b[2]*b[3]>=.38*bs[0][2]*bs[0][3]]
            left=min(b[0]-.20*b[2] for b in major); right=max(b[0]+1.20*b[2] for b in major)
            if right-left>crop_width*.96:
                wide+=1;counts['wide']+=1;continue
            # Track center of face/group with modest headroom on both sides.
            c=(left+right)/2; c=float(np.clip(c,crop_width/2,1-crop_width/2))
            centers.append(c);ids.append(i);counts['group' if len(major)>1 else 'single']+=1
        length=hi-lo
        shot_info.append({'start':round(lo/rate,3),'samples':length,'tracked':len(ids),'wide':wide,'small_edge':small_edge})
        # No interpolation across long missing intervals or wide groups. Prefer
        # preserving the whole clip to switching into a crop that loses people.
        gap=max(np.diff([lo-1,*ids,hi])) if ids else length+1
        possible_webcam |= small_edge>=max(2,math.ceil(length*.45))
        if length<2 or len(ids)<math.ceil(length*.65) or wide>max(0,int(length*.10)) or gap>max(3,round(rate*1.25)):
            needs_full=True;continue
        values=np.interp(np.arange(lo,hi),ids,centers)
        # Smooth within scene, never across cuts. Clamp so faces remain within
        # the crop even when someone moves quickly.
        sm=np.convolve(np.pad(values,(1,1),mode='edge'),[.20,.60,.20],mode='valid')
        for j,i in enumerate(range(lo,hi)):
            c=float(np.clip(sm[j],values[j]-.035,values[j]+.035));points.append([round(i/rate,4),round(c,5)])
    if needs_full:
        return {'method':'full-frame safety layout','kind':'full','reason':'No reliable narrow subject in every scene, or widely separated faces. Entire image retained.','scene_cuts':[round(i/rate,3) for i in cuts[1:]],'counts':counts,'shots':shot_info,'possible_webcam':possible_webcam}
    return {'method':'scene-aware face/group tracking','kind':'irl','track':points,'scene_cuts':[round(i/rate,4) for i in cuts[1:]],'counts':counts,'shots':shot_info,'support':round((counts['single']+counts['group'])/n,3)}


def analyze_subject(frames,rate,allow_body=True):
    scene=analyze_frames(frames,rate)
    if scene['kind']!='full' or scene.get('possible_webcam') or not allow_body:return scene
    try:
        from body_reframe import person_boxes, plan_body
        body=plan_body(frames,rate,person_boxes(frames),shot_boundaries(frames))
        if body:return body
    except Exception as e:
        scene['body_model_note']=str(e)[:300]
    return scene


def classify(source,meta,ffmpeg,run,estimate=False,allow_uncertain_full=False):
    # Preserve the proven webcam route. A stable border is positive evidence;
    # no webcam detection is not proof that no webcam exists.
    error=None; cam=None
    try:cam=detect(source,meta,ffmpeg,run,estimate)
    except DetectionReview as e:error=e
    if cam and not cam.get('approximate'):return cam
    frames,rate=sample_frames(source,meta,ffmpeg,run)
    scene=analyze_subject(frames,rate,allow_body=cam is None and isinstance(error,NoStableFace))
    if cam and cam.get('approximate'):
        # Approximate peripheral face regions are explicitly opt-in and still
        # need segmentation QC; a well-tracked ordinary scene takes priority.
        if scene['kind']=='irl':return scene
        return cam
    if error is not None and not isinstance(error,NoStableFace):
        if scene['kind']=='irl' and 'Multiple' not in str(error):return scene
        if not allow_uncertain_full:raise error
    if scene['kind']=='full' and scene.get('possible_webcam') and not allow_uncertain_full:
        raise DetectionReview('Small peripheral face may be a missed webcam. Draw a crop or enable uncertain full-view fallback.')
    if scene['kind']=='full':
        scene['webcam_absence_verified']=False
        if error:scene['webcam_scan_note']=str(error)
    return scene


def track_expr(points,cuts=(),offset=0):
    """Piecewise linear center trajectory; hard reset at sampled scene cuts.
    Balanced sum avoids deeply nested ffmpeg expression parser limits.
    """
    if not points:raise DetectionReview('Tracking needs to be analyzed first.')
    clock=f'(t+{float(offset):.6f})';cuts=set(round(float(c),4) for c in cuts)
    terms=[f'{float(points[0][1]):.6f}']
    for (ta,ca),(tb,cb) in zip(points,points[1:]):
        delta=cb-ca
        if abs(delta)<.00001:continue
        if round(tb,4) in cuts:f=f'gte({clock},{tb:.4f})'
        else:f=f'max(0,min(1,({clock}-{ta:.4f})/{max(.001,tb-ta):.4f}))'
        terms.append(f'({delta:.6f}*{f})')
    while len(terms)>1:terms=[f'({terms[i]}+{terms[i+1]})' if i+1<len(terms) else terms[i] for i in range(0,len(terms),2)]
    return terms[0]
