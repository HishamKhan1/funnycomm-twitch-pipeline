"""Offline speech-to-text with word timings (faster-whisper, CPU)."""
from __future__ import annotations
import os, re, threading
import numpy as np
from . import media

_lock = threading.Lock()
_model = {}
BEST = 'distil-whisper/distil-large-v3.5-ct2'
PREFER = [BEST, 'small.en', 'base.en', 'tiny.en', 'base', 'tiny']
NAMES = 'Lacy, Marlon, Ron, Ronaldo, Silky, Jason, Adapt, Core, rizz, Kai, Clix'


def installed():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _cached(name):
    try:
        from faster_whisper.utils import download_model
        return download_model(name, local_files_only=True)
    except Exception:
        return None


def pick_model(wanted='auto'):
    """Prefer a model already on this PC. Returns (name, path) or (None, None)."""
    if not installed():
        return None, None
    names = PREFER if wanted in ('', 'auto', None, 'best') else [wanted] + PREFER
    for n in names:
        p = _cached(n)
        if p:
            return n, p
    return None, None


def get_model(wanted='auto'):
    with _lock:
        if 'm' in _model:
            return _model['m'], _model['n']
        name, path = pick_model(wanted)
        if not path:
            return None, None
        from faster_whisper import WhisperModel
        threads = max(2, min(8, (os.cpu_count() or 4) - 2))
        try:
            _model['m'] = WhisperModel(path, device='cpu', compute_type='int8', cpu_threads=threads)
        except (MemoryError, RuntimeError):
            # Not enough free memory for the large model right now: use a smaller one.
            for n in PREFER[1:]:
                p = _cached(n)
                if p:
                    name, path = n, p
                    break
            _model['m'] = WhisperModel(path, device='cpu', compute_type='int8', cpu_threads=threads)
        _model['n'] = name
        return _model['m'], name


def transcribe(path, wanted='auto'):
    """-> {'ok', 'model', 'words': [[text,start,end,prob]], 'lines': [[start,end,text]], 'error'}"""
    model, name = get_model(wanted)
    if model is None:
        return {'ok': False, 'model': None, 'words': [], 'lines': [],
                'error': 'Speech recognition is not installed (run Install_Smart_Editing.bat).'}
    try:
        x = media.pcm(path, 16000)
        if len(x) < 8000:
            return {'ok': True, 'model': name, 'words': [], 'lines': [], 'error': None}
        with _lock:
            segs, info = model.transcribe(x, language='en', beam_size=3, word_timestamps=True, vad_filter=True,
                                          vad_parameters={'min_silence_duration_ms': 400, 'speech_pad_ms': 200, 'threshold': .3},
                                          condition_on_previous_text=False, no_speech_threshold=.65, hotwords=NAMES)
            words, lines = [], []
            for s in segs:
                media.check_cancel()
                if s.no_speech_prob > .75 and s.avg_logprob < -1.0:
                    continue
                if s.compression_ratio > 2.8:      # stuck-loop hallucination
                    continue
                txt = s.text.strip()
                ws = [[w.word.strip(), round(float(w.start), 2), round(float(w.end), 2), round(float(w.probability), 2)] for w in (s.words or []) if w.word.strip()]
                if not ws:
                    continue
                lines.append([ws[0][1], ws[-1][2], txt, round(float(np.mean([w[3] for w in ws])), 2)])
                words += ws
        return {'ok': True, 'model': name, 'words': words, 'lines': lines, 'error': None}
    except media.Cancelled:
        raise
    except Exception as e:
        return {'ok': False, 'model': name, 'words': [], 'lines': [], 'error': str(e)[:240]}


# ------------------------------------------------------------------ language
# Hate-slur patterns are muted by default; the rest only inform the reviewer.
_SLUR = re.compile(r"^(n[i1]gg(a|as|az|er|ers|uh|ah|ahs|let|lets|y)|n[i1]g(a|as|ah|uh|az)|neega|knigga|f[a4]gg?(ot|ots|s)?|tr[a4]nn(y|ies)|k[i1]ke(s)?|ch[i1]nk(s|y)?|sp[i1]c(s)?|"
                   r"wetback(s)?|gook(s)?|ret[a4]rd(s|ed)?|dyke(s)?|coon(s)?|beaner(s)?|paki(s)?)$", re.I)
_STRONG = re.compile(r"^(mother)?f+u+c+k|^(cunt|cocksuck|cock$|dickhead|pussy|whore|slut)", re.I)
_MILD = re.compile(r"^(shit|bullshit|bitch|ass$|asshole|damn|goddamn|dick|piss|tits)", re.I)
_SEXUAL = re.compile(r"^(sex|sexy|horny|porn|onlyfans|nude|nudes|naked|penis|vagina|boobs?|titties|dildo|blowjob|orgasm|cum|jerk(ing)?off|gooning|gooner|thirst|milf|hentai|nipples?)$", re.I)
_VIOLENT = re.compile(r"^(kill|killed|killing|murder|murdered|shoot|shooting|stab|stabbed|suicide|bomb|terrorist|rape|raped|rapist|hang)$", re.I)
_DRUG = re.compile(r"^(weed|cocaine|coke|meth|heroin|lsd|shrooms|xanax|percs?|fentanyl|blunt|joint)$", re.I)
_GAMBLE = re.compile(r"^(stake|roulette|blackjack|slots|gamba|casino|parlay)$", re.I)


def clean(w):
    return re.sub(r"[^A-Za-z0-9']", '', w).lower()


def language_scan(words):
    out = {'slur': [], 'strong': [], 'mild': [], 'sexual': [], 'violent': [], 'drugs': [], 'gambling': []}
    for w, s, e, p in words:
        c = clean(w)
        if not c:
            continue
        if _SLUR.match(c):
            out['slur'].append([s, e, p])
        elif _STRONG.match(c):
            out['strong'].append([s, e, p])
        elif _MILD.match(c):
            out['mild'].append([s, e, p])
        if _SEXUAL.match(c):
            out['sexual'].append([s, e, p])
        if _VIOLENT.match(c):
            out['violent'].append([s, e, p])
        if _DRUG.match(c):
            out['drugs'].append([s, e, p])
        if _GAMBLE.match(c):
            out['gambling'].append([s, e, p])
    return out


_CHAT_SLUR = re.compile(r"(?i)(n[i1!|l]+g+(a|er|uh|ah|as|az|ers)s?(?![a-z])|(?<![a-z])n+g+a+h?s?(?![a-z])|f[a@4]g+([o0]t)?s?(?![a-z])|(?<![a-z])tr[a@4]nn(y|ies)|r[e3]t[a@4]rd|k[i1]ke(?![a-z])|ch[i1]nk(?![a-z])|n[ae]{1,4}g{1,4}a+(?![a-z])|(fuck\w*|dirty|stupid|filthy|you|gas (the|all)) (fuck\w* )?(jews?|muslims?|blacks?|indians?|mexicans?|pakis?|arabs?)(?![a-z])|heil|hitler|sieg|(?<![a-z])kys(?![a-z])|kill yourself)")


def chat_has_slur(lines):
    return any(_CHAT_SLUR.search(t or '') for t in lines)


def title_has_slur(text):
    return any(_SLUR.match(clean(t)) for t in re.findall(r"[A-Za-z0-9']+", text or '')) or bool(re.search(r'\bn[ -]?word\b', text or '', re.I))
