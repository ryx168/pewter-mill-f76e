"""Narration synthesis for the tour promo pipeline.

  omnivoice : k2-fsa/OmniVoice voice clone of Ronnie (tour/assets/ronnie/ronnie_ref.wav) - the voice
              the 7622 video used (see assets/pdfs/7622/part*/part_*_tc.py). CPU is fine; the 3 GB
              model loads once per run, so every line is produced in one process.
  edge      : edge-tts zh-TW-HsiaoChenNeural - no model, no GPU, no reference; the fallback.

Both write 24 kHz mono PCM and are cached by (provider, voice, text) so re-running a build only
synthesizes lines that changed. Output per line: raw s16le bytes trimmed of leading/trailing silence.
"""
import array, hashlib, os, subprocess, sys, wave

SR = 24000
EDGE_VOICE = "zh-TW-HsiaoChenNeural"
OMNI_MODEL = os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")

def log(*a):
    print(*a, flush=True)

def _to_simplified(text):
    """OmniVoice was always driven with Simplified text in this repo; keep that, display stays Traditional."""
    try:
        import opencc
        return opencc.OpenCC("t2s").convert(text)
    except Exception:
        return text

def trim_silence(pcm, thresh=500, keep=0.12):
    a = array.array("h"); a.frombytes(pcm)
    n = len(a); i, j = 0, n
    while i < n and abs(a[i]) < thresh: i += 1
    while j > i and abs(a[j - 1]) < thresh: j -= 1
    pad = int(keep * SR)
    return a[max(0, i - pad):min(n, j + pad)].tobytes()

def _mp3_to_raw(mp3, raw):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp3, "-f", "s16le", "-ar", str(SR), "-ac", "1", raw], check=True)

class Narrator:
    def __init__(self, provider, cache_dir, ref_audio=None, speed=1.05, edge_voice=EDGE_VOICE, force=False):
        self.provider = provider
        self.cache = cache_dir
        self.ref = ref_audio
        self.speed = speed
        self.edge_voice = edge_voice
        self.force = force
        self._model = None
        os.makedirs(cache_dir, exist_ok=True)
        if provider == "omnivoice" and not (ref_audio and os.path.exists(ref_audio)):
            log(f"  omnivoice: reference audio missing ({ref_audio}); falling back to edge-tts")
            self.provider = "edge"

    def _key(self, text):
        ident = f"{self.provider}|{self.edge_voice if self.provider == 'edge' else os.path.basename(self.ref or '')}|{self.speed}|{text}"
        return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:14]

    def line(self, text):
        """-> (raw_pcm_bytes, provider_used)"""
        raw = os.path.join(self.cache, self._key(text) + ".raw")
        if not self.force and os.path.exists(raw) and os.path.getsize(raw) > 0:
            return open(raw, "rb").read(), self.provider
        used = self.provider
        if self.provider == "omnivoice":
            try:
                pcm = self._omnivoice(text)
            except Exception as e:
                log(f"  omnivoice failed ({type(e).__name__}: {str(e)[:120]}); edge-tts for this line")
                pcm, used = self._edge(text), "edge"
        else:
            pcm = self._edge(text)
        pcm = trim_silence(pcm)
        with open(raw, "wb") as f:
            f.write(pcm)
        return pcm, used

    def _edge(self, text):
        key = self._key(text) + "_edge"
        mp3 = os.path.join(self.cache, key + ".mp3"); raw = os.path.join(self.cache, key + ".raw")
        for attempt in range(3):
            try:
                subprocess.run([sys.executable, "-m", "edge_tts", "--voice", self.edge_voice, "--rate", "+2%",
                                "--text", text, "--write-media", mp3], check=True)
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
        _mp3_to_raw(mp3, raw)
        return open(raw, "rb").read()

    def _omnivoice(self, text):
        import numpy as np, torch
        if self._model is None:
            log(f"  loading {OMNI_MODEL} ...")
            torch.set_num_threads(os.cpu_count() or 4)
            from omnivoice import OmniVoice
            from omnivoice.models.omnivoice import OmniVoiceGenerationConfig
            self._model = OmniVoice.from_pretrained(OMNI_MODEL)
            # the reference is encoded ONCE, not per line - that was most of the cost. A sidecar
            # <ref>.txt transcript skips Whisper (a 1.5 GB download on a runner).
            ref_txt = os.path.splitext(self.ref)[0] + ".txt"
            ref_text = open(ref_txt, encoding="utf-8").read().strip() if os.path.exists(ref_txt) else None
            if ref_text:
                log(f"  reference transcript: {ref_txt}")
                self._prompt = self._model.create_voice_clone_prompt(ref_audio=self.ref, ref_text=ref_text)
            else:
                if hasattr(self._model, "load_asr_model"):
                    self._model.load_asr_model()
                self._prompt = self._model.create_voice_clone_prompt(ref_audio=self.ref)
            self._cfg = OmniVoiceGenerationConfig(num_step=int(os.environ.get("OMNIVOICE_STEPS", "32")))
        audio = self._model.generate(text=_to_simplified(text), voice_clone_prompt=self._prompt,
                                     speed=self.speed, generation_config=self._cfg)
        a = np.asarray(audio[0], dtype="float32")
        a = np.clip(a, -1.0, 1.0)
        return (a * 32767).astype("<i2").tobytes()

def write_wav(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)
