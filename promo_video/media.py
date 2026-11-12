"""Media providers for the tour promo pipeline. Every provider is best-effort and returns None on
failure so the caller can fall back - a missing API key or an exhausted quota must never stop a
render, it only changes which pictures the video is made of.

  images  : gemini (GEMINI_API_KEY)  ->  brochure photos on the live site  ->  the tour's day images
  presenter: gemini image edit with the Ronnie green-screen headshot -> local chroma-key cutout
  clips   : pexels (PEXELS_API_KEY)  ->  none (stills get Ken Burns instead)
  intro   : the tour's own HLS intro (videos.xotours.ca), always public
  music   : yt-dlp from a YouTube URL  ->  a local file  ->  none
"""
import base64, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request

UA = "Mozilla/5.0 (xotours-promo-video; +https://xotours.net)"
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

def log(*a):
    print(*a, flush=True)

def fetch(url, dest=None, timeout=90, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if dest:
        with open(dest, "wb") as f:
            f.write(data)
    return data

def ffmpeg(*args, quiet=True):
    cmd = ["ffmpeg", "-v", "error" if quiet else "info", "-y", *args]
    subprocess.run(cmd, check=True)

def ffprobe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0

def fit_image(src, dst, w=1920, h=1080):
    """Cover-fit to w x h (crop), JPEG. Falls back to ffmpeg when Pillow is absent."""
    try:
        from PIL import Image, ImageOps
        im = Image.open(src).convert("RGB")
        im = ImageOps.fit(im, (w, h), Image.LANCZOS, centering=(0.5, 0.45))
        im.save(dst, "JPEG", quality=88)
    except ImportError:
        ffmpeg("-i", src, "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}", "-q:v", "3", dst)
    return dst

# ---------------------------------------------------------------- Gemini images
def gemini_image(prompt, dest, api_key, ref_images=(), aspect="16:9", retries=2):
    """One generateContent call with IMAGE output. Returns dest or None (402 / 429 / refusal)."""
    if not api_key:
        return None
    parts = [{"text": prompt}]
    for p in ref_images:
        with open(p, "rb") as f:
            b = base64.b64encode(f.read()).decode()
        mime = "image/png" if p.lower().endswith(".png") else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": b}})
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"responseModalities": ["IMAGE"],
                                            "imageConfig": {"aspectRatio": aspect}}}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent?key={api_key}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            log(f"  gemini {e.code}: {msg}")
            if e.code in (402, 400, 403):
                return None            # billing / key problem: no point retrying
            if attempt < retries:
                time.sleep(8 * (attempt + 1)); continue
            return None
        except Exception as e:
            log(f"  gemini error: {e}")
            if attempt < retries:
                time.sleep(5); continue
            return None
        for cand in d.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                blob = part.get("inlineData") or part.get("inline_data")
                if blob and blob.get("data"):
                    raw = dest + ".raw"
                    with open(raw, "wb") as f:
                        f.write(base64.b64decode(blob["data"]))
                    fit_image(raw, dest); os.remove(raw)
                    return dest
        log("  gemini returned no image (" + str(d.get("promptFeedback", ""))[:120] + ")")
        return None

# ---------------------------------------------------------------- brochure photos (live site)
def brochure_page(site, tid, cache_dir):
    """Fetch brochure-<id>.html (or the tour JSON's hand-named page) once; return html or ''."""
    dst = os.path.join(cache_dir, f"brochure_{tid}.html")
    if os.path.exists(dst):
        return open(dst, encoding="utf-8").read()
    # the tour page carries the id -> brochure page map (generated + hand-named pages)
    cands = []
    try:
        page = fetch(f"{site}/{tid}/", timeout=60).decode("utf-8", "replace")
        m = re.search(r"XO_BROCHURE_PAGES=/\*XO_BROCHURE_PAGES\*/(\{.*?\})/\*END", page, re.S)
        if m:
            mapped = json.loads(m.group(1)).get(str(tid))
            if mapped:
                cands.append(site + "/" + mapped.lstrip("/"))
    except Exception as e:
        log(f"  tour page map unavailable: {e}")
    cands.append(f"{site}/brochure-{tid}.html")
    for cand in cands:
        try:
            html = fetch(cand).decode("utf-8", "replace")
            if "brochure" in html and "<section" in html:
                open(dst, "w", encoding="utf-8").write(html); return html
        except Exception:
            pass
    return ""

def brochure_photos(html, site, tid):
    """Return {section: [absolute jpg urls]} for the brochure's own photos, per section."""
    out = {}
    if not html:
        return out
    for m in re.finditer(r'<section class="sec[^"]*" id="([a-z]+)"[\s\S]*?</section>', html):
        sec = m.group(1)
        urls = []
        for u in re.findall(r'(?:src|srcset)="([^"\s]*brochure/[a-z0-9_]+\.jpg)', m.group(0)):
            u = u if u.startswith("http") else site + "/" + u.lstrip("/")
            if u not in urls:
                urls.append(u)
        if urls:
            out[sec] = urls
    allu = []
    for u in re.findall(r'(?:src|srcset)="([^"\s]*brochure/[a-z0-9_]+\.jpg)', html):
        u = u if u.startswith("http") else site + "/" + u.lstrip("/")
        if u not in allu and not re.search(r"/(cover_lqip|poster\d_bg|cover_bg|cover)\.jpg$", u):
            allu.append(u)
    out["_all"] = allu
    return out

def brochure_day_photos(html, site):
    """{day_number: [(url, caption)]} from the brochure's <article class="day" id="dayN"> cards."""
    out = {}
    for m in re.finditer(r'<article class="day"[^>]*id="day(\d+)"[\s\S]*?</article>', html or ""):
        day = int(m.group(1)); items = []
        for u, alt in re.findall(r'<img src="([^"]*brochure/[a-z0-9_]+\.jpg)"[^>]*alt="([^"]*)"', m.group(0)):
            u = u if u.startswith("http") else site + "/" + u.lstrip("/")
            items.append((u, alt))
        if items:
            out[day] = items
    return out

def brochure_reasons(html):
    """The 8 必選理由 as [(no, title, [bullets])]."""
    items = []
    for n, h, ul in re.findall(r'class="rn">(\d+)</span><div><h[34]>([^<]*)</h[34]>\s*<ul>([\s\S]*?)</ul>', html or ""):
        lis = [re.sub(r"\s+", " ", x).strip() for x in re.findall(r"<li>([^<]*)", ul)]
        items.append((n, h.strip(), lis))
    return items

def brochure_route_svg(html):
    m = re.search(r'<svg[^>]*class="route-map"[\s\S]*?</svg>', html or "")
    return m.group(0) if m else ""

# ---------------------------------------------------------------- Pexels stock clips
def pexels_clip(query, dest, api_key, seconds=8):
    if not api_key:
        return None
    try:
        q = urllib.parse.quote(query)
        d = json.loads(fetch(f"https://api.pexels.com/videos/search?query={q}&orientation=landscape&size=medium&per_page=8",
                             headers={"Authorization": api_key}).decode())
    except Exception as e:
        log(f"  pexels search failed ({query}): {e}")
        return None
    best = None
    for v in d.get("videos", []):
        if v.get("duration", 0) < 5:
            continue
        files = [f for f in v.get("video_files", []) if f.get("width", 0) >= 1280 and f.get("file_type") == "video/mp4"]
        files.sort(key=lambda f: abs(f.get("width", 0) - 1920))
        if files:
            best = files[0]["link"]; break
    if not best:
        return None
    try:
        ffmpeg("-user_agent", UA, "-i", best, "-t", str(seconds), "-an",
               "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", dest)
        return dest
    except subprocess.CalledProcessError:
        return None

# ---------------------------------------------------------------- the tour's own HLS intro
def hls_clip(video_src, dest, seconds=14, max_h=1080, offset=0.0):
    if not video_src or not video_src.startswith("http"):
        return None
    try:
        master = fetch(video_src).decode("utf-8", "replace")
    except Exception as e:
        log(f"  intro skipped ({e})"); return None
    best, best_h = None, 0
    lines = master.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        if m and i + 1 < len(lines) and best_h < int(m.group(2)) <= max_h:
            best_h, best = int(m.group(2)), lines[i + 1].strip()
    src = video_src if not best else video_src.rsplit("/", 1)[0] + "/" + best
    for off in (offset, 0.0):          # a short intro may not reach the offset: retry from the start
        try:
            ffmpeg("-user_agent", UA, "-ss", str(off), "-i", src, "-t", str(seconds), "-an",
                   "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", dest)
            if ffprobe_duration(dest) >= min(seconds, 4):
                return dest
        except subprocess.CalledProcessError as e:
            log(f"  intro at {off}s failed (ffmpeg {e.returncode})")
    return None

# ---------------------------------------------------------------- music
def youtube_audio(url, dest_base):
    """yt-dlp -> mp3. Returns path or None. (YouTube blocks many datacenter IPs - Actions may fail.)"""
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "-f", "ba", "-x", "--audio-format", "mp3", "--audio-quality", "4",
                        "--no-playlist", "-o", dest_base + ".%(ext)s", url], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception as e:
        log(f"  yt-dlp failed: {e}"); return None
    p = dest_base + ".mp3"
    return p if os.path.exists(p) else None

# ---------------------------------------------------------------- presenter cutout
def chroma_cutout(src, dest):
    """Green-screen headshot -> RGBA PNG with the background removed (PIL, no model)."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    green = (g > 120) & (g > r + 40) & (g > b + 40)
    edge = (g > 90) & (g > r + 15) & (g > b + 15) & ~green
    alpha = np.where(green, 0, np.where(edge, 110, 255)).astype("uint8")
    # despill: pull green down toward the mean of r and b on kept pixels
    g2 = np.where(alpha > 0, np.minimum(g, (r + b) // 2 + 25), g)
    rgba = np.dstack([r, g2, b, alpha]).astype("uint8")
    Image.fromarray(rgba, "RGBA").save(dest)
    return dest
