"""Optional commentary. Your own recording is best; Windows' built-in voice is the free fallback."""
from __future__ import annotations
import os, subprocess, tempfile
from pathlib import Path
from . import media


def voices():
    if os.name != 'nt':
        return []
    cmd = "Add-Type -AssemblyName System.Speech;(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices()|%{$_.VoiceInfo.Name}"
    p = subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True, creationflags=media.NOWIN)
    return [x.strip() for x in p.stdout.splitlines() if x.strip()]


def speak(text, target, voice='', rate=1):
    """Render text to WAV with System.Speech. Text travels by file, never on the command line."""
    if os.name != 'nt':
        raise media.MediaError('Built-in voice needs Windows. Choose a recording instead.')
    with tempfile.TemporaryDirectory(prefix='fc_v_') as td:
        t, s, o = Path(td) / 't.txt', Path(td) / 's.ps1', Path(td) / 'o.wav'
        t.write_text(text, encoding='utf-8')
        s.write_text("param($t,$o,$v,$r)\nAdd-Type -AssemblyName System.Speech\n$y=New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                     "if($v){try{$y.SelectVoice($v)}catch{}}\n$y.Rate=[int]$r\n$y.SetOutputToWaveFile($o)\n"
                     "$y.Speak([IO.File]::ReadAllText($t,[Text.Encoding]::UTF8))\n$y.Dispose()\n", encoding='utf-8')
        p = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(s), '-t', str(t), '-o', str(o), '-v', voice, '-r', str(int(rate))],
                           capture_output=True, text=True, creationflags=media.NOWIN, timeout=120)
        if not o.is_file() or o.stat().st_size < 2000:
            raise media.MediaError('Windows voice could not be created: ' + (p.stderr or '')[-200:])
        Path(target).write_bytes(o.read_bytes())
    return target
