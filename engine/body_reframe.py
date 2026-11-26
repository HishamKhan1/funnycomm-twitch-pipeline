"""Conservative body-based framing using the existing local selfie model."""
import math
import cv2
import numpy as np
from clip_types import MODEL, available


def mask_box(mask):
    # Use high-confidence foreground; suppress tiny fragments and compression noise.
    m=np.asarray(mask,dtype=np.float32)
    if m.ndim!=2 or not np.isfinite(m).all():return None
    h,w=m.shape
    binary=(m>=.65).astype(np.uint8)
    binary=cv2.morphologyEx(binary,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    n,labels,stats,_=cv2.connectedComponentsWithStats(binary,8)
    major=[s for s in stats[1:] if s[cv2.CC_STAT_AREA]>=.025*h*w]
    if not major:return None
    major=sorted(major,key=lambda s:s[cv2.CC_STAT_AREA],reverse=True)
    if len(major)>1 and major[1][cv2.CC_STAT_AREA]>.32*major[0][cv2.CC_STAT_AREA]:return None
    x,y,a,b,area=map(int,major[0])
    if area<.065*h*w or area>.78*h*w or b<.30*h or a<.11*w or a>.74*w:return None
    if area/(a*b)<.30:return None
    return (x/w,y/h,a/w,b/h)


def person_boxes(frames):
    if not available():return None
    import mediapipe as mp
    opts=mp.tasks.vision.ImageSegmenterOptions(base_options=mp.tasks.BaseOptions(model_asset_path=str(MODEL)),running_mode=mp.tasks.vision.RunningMode.IMAGE,output_category_mask=False,output_confidence_masks=True)
    out=[]
    with mp.tasks.vision.ImageSegmenter.create_from_options(opts) as model:
        for frame in frames:
            rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
            result=model.segment(mp.Image(image_format=mp.ImageFormat.SRGB,data=rgb))
            if not result.confidence_masks or len(result.confidence_masks)<2:out.append(None);continue
            m=result.confidence_masks[1].numpy_view().copy()
            out.append(mask_box(m))
    return out


def plan_body(frames,rate,boxes,cuts):
    if boxes is None or len(boxes)!=len(frames):return None
    h,w=frames[0].shape[:2];n=len(frames);minimum=h*9/16/w
    # Fit whole detected body horizontally with 8% margin on each side. A
    # slightly wider portrait foreground is preferable to cutting off the body.
    required=[min(1.,b[2]*1.16) for b in boxes if b is not None]
    if not required:return None
    width=max(minimum,max(required))
    if width>.78:return None
    points=[];shot_info=[];total=0
    for lo,hi in zip(cuts,cuts[1:]+[n]):
        ids=[i for i in range(lo,hi) if boxes[i] is not None];length=hi-lo
        if length<2 or len(ids)<math.ceil(length*.70):return None
        if max(np.diff([lo-1,*ids,hi]))>max(3,round(rate*1.0)):return None
        # Avoid mistaken switches between disconnected foreground objects.
        centers=[boxes[i][0]+boxes[i][2]/2 for i in ids]
        if any(abs(b-a)>.33 for a,b in zip(centers,centers[1:])):return None
        values=np.interp(np.arange(lo,hi),ids,centers)
        sm=np.convolve(np.pad(values,(1,1),mode='edge'),[.18,.64,.18],mode='valid')
        for j,i in enumerate(range(lo,hi)):
            c=float(np.clip(sm[j],values[j]-.025,values[j]+.025))
            b=boxes[i]
            if b:
                # Clamp the crop center so the detected foreground stays in view.
                c=float(np.clip(c,b[0]+b[2]-width/2,b[0]+width/2))
            c=float(np.clip(c,width/2,1-width/2));points.append([round(i/rate,4),round(c,5)])
        total+=len(ids);shot_info.append({'start':round(lo/rate,3),'samples':length,'tracked':len(ids)})
    return {'method':'scene-aware person/body tracking','kind':'irl','track':points,'track_width':round(width,5),'scene_cuts':[round(i/rate,4) for i in cuts[1:]],'support':round(total/n,3),'shots':shot_info,'body_model':'local selfie segmentation','wide_subject_padding':width>minimum+.005}
