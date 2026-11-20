#!/usr/bin/env python
"""Tour promotion video - the 7622 pipeline, automated.

The 7622 video (assets/pdfs/7622/) was made by hand: Gemini images and clips, Google Vids stock
footage, a Movavi slideshow, Ronnie's cloned voice from OmniVoice, YouTube music, the brochure
look. This script does the same steps from the tour's public data so it runs on a GitHub runner:

    python build.py --id 7622 plan      # tour JSON + live brochure page -> plan.json (shots, reasons, narration)
    python build.py --id 7622 media     # Gemini / brochure photos / Pexels / HLS intro / logo / QR / presenter
    python build.py --id 7622 voice     # OmniVoice (Ronnie) or edge-tts -> assets/vo/*.wav + timing
    python build.py --id 7622 music --youtube <url>   # yt-dlp -> assets/bgm.mp3 (optional)
    python build.py --id 7622 compose   # -> index.html (HyperFrames composition)
    npx --yes hyperframes@0.5.1 render -q standard -o renders/promo_7622.mp4
    python build.py --id 7622 finish renders/promo_7622.mp4   # music + sfx + loudnorm -> out/

`python build.py --id 7622 all --youtube <url>` runs plan..compose. Every external source is
optional and falls back (see media.py); the only hard inputs are the tour JSON and ffmpeg.
"""
import argparse, html, json, os, re, shutil, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOUR_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import media, voice as voicemod

ASSETS = os.path.join(HERE, "assets")
ART = os.path.join(ASSETS, "art")
VO = os.path.join(ASSETS, "vo")
OUT = os.path.join(HERE, "out")
for d in (ART, VO, OUT, os.path.join(ASSETS, "cache")):
    os.makedirs(d, exist_ok=True)
RONNIE = os.path.join(TOUR_ROOT, "assets", "ronnie")
PLAN = os.path.join(HERE, "plan.json")
TIMING = os.path.join(HERE, "timing.json")
GAP = 0.42
MAX_PORTS = 7

def log(*a):
    print(*a, flush=True)

def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def save_json(p, d):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)

def cjk_only(s):
    return re.sub(r"[（(][^）)]*[）)]", "", s or "").replace("・", "").strip()

def split_stop(name):
    """'康瓦爾郡・法爾茂斯 Falmouth' -> zh, en"""
    name = re.sub(r"\s+", " ", (name or "").strip())
    m = re.match(r"^(.*?[㐀-鿿　-〿＀-￯）)])\s*([A-Za-z][A-Za-z /&'.\-]*)?$", name)
    if m:
        return m.group(1).strip(" ・"), (m.group(2) or "").strip()
    return name, ""

CN = "零一二三四五六七八九"
def phone_display(p):
    return re.sub(r"[.\s]+", "-", (p or "").strip())
def phone_speech(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11:
        groups = [d[:1], d[1:4], d[4:7], d[7:]]
    elif len(d) == 10:
        groups = [d[:3], d[3:6], d[6:]]
    else:
        groups = re.findall(r"\d{1,4}", d)
    return "，".join("".join(CN[int(c)] for c in g) for g in groups if g)

# ================================================================ plan
def load_tour(tid, site):
    local = os.path.join(TOUR_ROOT, "kv_json_exports", f"tour_{tid}.json")
    if os.path.exists(local):
        return load_json(local), local
    url = f"{site}/kv_json_exports/tour_{tid}.json"
    return json.loads(media.fetch(url).decode("utf-8")), url

def first_sentence(desc, zh):
    """A short descriptive sentence for a port, from the day's own text (skip port-hours boilerplate)."""
    for s in re.split(r"[。！；\n]", desc or ""):
        s = s.strip("・ ")
        if not s or re.match(r"^(靠港|抵達|酒店早餐|早餐後|今天|今日|約)", s) or "自費" in s or "岸上行程" in s:
            continue
        if 8 <= len(s) <= 42:
            return s + "。"
    return f"走訪{zh}。"

def ship_name(t):
    for src in (t.get("eventNameFull", ""), t.get("eventNameShort", ""), " ".join(t.get("hotelInfoList") or [])):
        m = re.search(r"([一-鿿]{2,8}號)(郵輪|遊輪)?", src)
        if m:
            return m.group(1) + (m.group(2) or "")
    for tag in t.get("tags") or []:
        n = tag.get("name", "") if isinstance(tag, dict) else str(tag)
        if "號" in n:
            return n
    return ""

def plan(args):
    site = args.site.rstrip("/")
    tid = str(args.id)
    t, src = load_tour(tid, site)
    log(f"tour {tid} <- {src}")
    itin = t.get("itinerary") or []
    bro_html = media.brochure_page(site, tid, os.path.join(ASSETS, "cache"))
    log(f"brochure page: {'yes' if bro_html else 'none'}")
    photos = media.brochure_photos(bro_html, site, tid)
    reasons = media.brochure_reasons(bro_html)
    route_svg = media.brochure_route_svg(bro_html)

    # ports: itinerary days with stops; flights, sea days and the embarkation port's repeats excluded
    bullets = [b for r in reasons for b in r[2]]
    used_bullets = set()
    def port_line(zh, desc):
        keys = [k for k in re.split(r"[・（）() ]", zh) if len(k) >= 2]
        best = None
        for b in bullets:                                   # the brochure's own copy first, each bullet once
            if b in used_bullets or not (8 <= len(b) <= 52):
                continue
            pos = min((b.find(k) for k in keys if k in b), default=-1)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, b)
        if best:
            used_bullets.add(best[1]); return best[1].rstrip("。") + "。"
        return first_sentence(desc, cjk_only(zh))
    ports, seen = [], set()
    for i, d in enumerate(itin):
        title = d.get("title") or ""
        stops = [s for s in (d.get("stops") or []) if s.get("name")]
        if not stops or "✈" in title:
            continue
        zh, en = split_stop(stops[0]["name"])
        if re.search(r"海上|At Sea|巡遊|登船|下船|Embark|Disembark", zh + en):
            continue
        city = re.split(r"[・（(\s]", zh)[0][:4]
        if city in seen:
            continue
        seen.add(city)
        ports.append(dict(day=i + 1, zh=zh, en=en or "", speech=cjk_only(zh), image=d.get("image"),
                          line=port_line(zh, d.get("description")), lat=stops[0].get("lat"), lng=stops[0].get("lng")))
    while len(ports) > MAX_PORTS:
        # drop a transit port (embark / disembark) before dropping a destination
        victim = next((p for p in ports if re.search(r"登船|下船|Southampton|南安普敦", p["zh"] + p["en"])), None) or ports[len(ports) // 2]
        ports.remove(victim)
    # a photo per port: the brochure's own day-card photo (caption naming the port first), else by filename
    pool = list(photos.get("_all", []))
    day_photos = media.brochure_day_photos(bro_html, site)
    def pick_photo(p):
        cands = day_photos.get(p["day"], [])
        zkeys = [k for k in re.split(r"[・（）() ]", p["zh"]) if len(k) >= 2]
        for u, alt in sorted(cands, key=lambda ua: 0 if any(k in ua[1] for k in zkeys) else 1):
            if u in pool:
                pool.remove(u); return u
        keys = [k for k in re.split(r"[\s/]+", p["en"].lower()) if len(k) > 3]
        for u in pool:
            fn = u.rsplit("/", 1)[-1].lower()
            if any(k in fn for k in keys):
                pool.remove(u); return u
        return None
    for p in ports:
        p["photo"] = pick_photo(p)
    ship_photos = [u for u in photos.get("train", []) if re.search(r"(cabin|balcony|atrium|ship|deck|dining|lobster)", u)]
    ship = ship_name(t)
    brand_cn = t.get("brandNameCn") or "新聯華假期"
    brand_en = t.get("brandNameEn") or "XO TOURS"
    tag = next((x.get("name", "") for x in (t.get("tags") or []) if isinstance(x, dict)), "")
    short = t.get("eventNameShort") or t.get("eventNameFull") or f"行程 {tid}"
    date = (t.get("dateShort") or "").strip()
    dur = (t.get("durationText") or "").strip()
    inc = [s.strip() for s in (t.get("includesList") or []) if s and s.strip()]
    price = t.get("price") or next((p.get("price") for p in (t.get("prices") or []) if p.get("price")), None)
    c1, c2 = t.get("contact1Phone") or "", t.get("contact2Phone") or ""
    c1c, c2c = (t.get("contact1Country") or "").strip(), (t.get("contact2Country") or "").strip()

    # ---- narration (Traditional on screen; voice.py converts to Simplified for OmniVoice)
    N = {}
    N["hero"] = [f"慶祝{brand_cn}{tag}！" if tag else f"{brand_cn}誠摯推薦！",
                 "，".join(x for x in (date, ship, short) if x) + "！"]
    if inc:
        N["hero"].append("、".join(inc[:4]) + "，通通包含！")
    r_titles = [r[1] for r in reasons]
    if r_titles:
        N["reasons1"] = ["必選理由，" + r_titles[0] + "。"] + [x + "。" for x in r_titles[1:4]]
        if len(r_titles) > 4:
            N["reasons2"] = [x + "。" for x in r_titles[4:8]]
    if ports:
        names = [p["speech"] for p in ports]
        if len(names) >= 3:
            N["route"] = [f"從{names[0]}啟航，途經{'、'.join(names[1:-1])}，最後抵達{names[-1]}。"]
        else:
            N["route"] = ["航線走訪" + "、".join(names) + "。"]
        N["ports"] = [p["line"] for p in ports]
    ship_lines = []
    for r in reasons:
        if re.search(r"艙|郵輪|遊輪|號", r[1]) and "船票" not in r[1]:
            ship_lines.append((r[2][0] if r[2] else r[1]).rstrip("。") + "。")
    ship_lines = ship_lines[:2]
    if not ship_lines and ship:
        ship_lines = [f"全程入住{ship}，推開落地窗就是海景。"]
    if ship_lines:
        N["ship"] = ship_lines
    N["price"] = [f"雙人房每位只要美金{int(float(price))}起！" if price else "價格請洽詢！"]
    if inc:
        N["price"].append("包含" + "、".join(inc[:5]) + "。")
    N["cta"] = ["名額有限，立即行動！"]
    if c1:
        N["cta"].append(f"立即致電{c1c}專線，{phone_speech(c1)}。")
    N["cta"].append("官方網站，xotours.net。")

    P = dict(
        tourId=tid, site=site, url=f"{site}/{tid}/", urlShort=f"xotours.net/{tid}/",
        brandCn=brand_cn, brandEn=brand_en, tag=tag, titleShort=short, titleFull=t.get("eventNameFull") or short,
        date=date, dateFull=t.get("dateFull") or "", duration=dur, ship=ship, price=price,
        prices=[p for p in (t.get("prices") or []) if p.get("price")][:3],
        includes=inc, phones=[dict(country=c1c, phone=phone_display(c1)), dict(country=c2c, phone=phone_display(c2))],
        cover=t.get("image") or t.get("videoPoster"), coverUrl=None, videoSrc=t.get("videoSrc"),
        reasons=[dict(no=r[0], title=r[1], bullets=r[2]) for r in reasons],
        posterBg=[u for u in photos.get("reasons", []) if "poster" in u],
        coverBg=next((u for u in photos.get("_all", []) + sum(photos.values(), []) if "cover_bg" in u), None),
        ports=ports, shipPhotos=ship_photos[:4], routeSvg=route_svg, narration=N, days=len(itin),
        dayTitles=[re.sub(r"\s+", " ", d.get("title") or "").strip() for d in itin],
        seoDesc=t.get("seo_desc") or "", keywords=(t.get("seo_keywords") or ""),
    )
    # the brochure cover_bg sits inside the cover header, not a <section>: find it directly
    m = re.search(r'([^"\s]*brochure/cover_bg\.jpg)', bro_html or "")
    if m:
        u = m.group(1); P["coverBg"] = u if u.startswith("http") else site + "/" + u.lstrip("/")
    for i, u in enumerate(re.findall(r'([^"\s]*brochure/poster\d_bg\.jpg)', bro_html or "")[:2]):
        P["posterBg"].append(u if u.startswith("http") else site + "/" + u.lstrip("/"))
    P["posterBg"] = list(dict.fromkeys(P["posterBg"]))
    save_json(PLAN, P)
    log(f"plan: {len(ports)} ports, {len(reasons)} reasons, {len(ship_photos)} ship photos, route map {'yes' if route_svg else 'no'}")
    for k, v in N.items():
        log(f"  {k}: " + " / ".join(v))

# ================================================================ media
def media_stage(args):
    P = load_json(PLAN)
    site = P["site"]
    gem = os.environ.get("GEMINI_API_KEY", "").strip() if args.images in ("auto", "gemini") else ""
    pex = os.environ.get("PEXELS_API_KEY", "").strip() if args.clips in ("auto", "pexels") else ""
    log(f"media: images={'gemini' if gem else 'brochure/site'} clips={'pexels' if pex else 'none'}")
    def art(name):
        return os.path.join(ART, name)
    def rel(p):
        return "assets/art/" + os.path.basename(p) if p else None
    def dl_image(url, name):
        dst = art(name)
        if os.path.exists(dst):
            return dst
        try:
            raw = dst + ".raw"; media.fetch(url, raw); media.fit_image(raw, dst); os.remove(raw); return dst
        except Exception as e:
            log(f"  image failed {url}: {e}"); return None

    # logo + QR
    logo = art("logo.png")
    if not os.path.exists(logo):
        src = os.path.join(TOUR_ROOT, "includes", "logo_gold.png")
        if os.path.exists(src):
            shutil.copy(src, logo)
        else:
            media.fetch(f"{site}/includes/logo_gold.png", logo)
    qr = art("qr.png")
    if not os.path.exists(qr):
        try:
            import qrcode
            qrcode.make(P["url"], border=1).save(qr)
        except ImportError:
            alt = os.path.join(RONNIE, "qrcode.png")
            if os.path.exists(alt):
                shutil.copy(alt, qr)
            else:
                qr = None
    # cover: brochure composite first, then the tour's own cover
    cover = None
    if P.get("coverBg"):
        cover = dl_image(P["coverBg"], "cover.jpg")
    if not cover and P.get("cover"):
        cover = dl_image(P["cover"] if P["cover"].startswith("http") else site + "/" + P["cover"].lstrip("/"), "cover.jpg")
    posters = [dl_image(u, f"poster{i+1}.jpg") for i, u in enumerate(P.get("posterBg", [])[:2])]

    # ports: Gemini image -> brochure photo -> day image; optional Pexels clip
    hero_hint = ""
    for i, p in enumerate(P["ports"]):
        name = f"port{p['day']}"
        img = art(name + ".jpg")
        if not os.path.exists(img):
            got = None
            if gem:
                prompt = (f"Photorealistic cinematic travel photograph of {p['en'] or p['zh']}, its most famous landmark "
                          f"or waterfront in view, golden hour light, gentle motion in the scene, wide 16:9 composition, "
                          f"ultra detailed, travel documentary look. No text, no captions, no logo, no watermark, no people in the foreground.")
                got = media.gemini_image(prompt, img, gem)
                if not got:
                    gem = ""   # one failure (402/403) means every call will fail: stop paying for round trips
            if not got and p.get("photo"):
                got = dl_image(p["photo"], name + ".jpg")
            if not got and p.get("image"):
                got = dl_image(p["image"] if p["image"].startswith("http") else site + "/" + p["image"].lstrip("/"), name + ".jpg")
            img = got
        p["img"] = rel(img)
        if i == 0 and p["en"]:
            hero_hint = p["en"]
        clip = art(name + ".mp4")
        if not os.path.exists(clip) and pex:
            media.pexels_clip(f"{p['en'] or p['zh']} travel", clip, pex)
        p["clip"] = rel(clip) if os.path.exists(clip) else None
        p["clipDur"] = round(media.ffprobe_duration(clip), 2) if p["clip"] else 0
        log(f"  {name}: img={'ok' if img else 'NONE'} clip={'ok' if p['clip'] else '-'}")

    # ship photos
    ships = []
    for i, u in enumerate(P.get("shipPhotos", [])):
        got = dl_image(u, f"ship{i+1}.jpg")
        if got:
            ships.append(rel(got))

    # presenter: Gemini scene with the Ronnie headshot as reference -> chroma cutout overlay
    pres_scene, pres_cut = art("presenter_scene.jpg"), art("presenter.png")
    ref = os.path.join(RONNIE, "red.png")
    if not os.path.exists(pres_scene) and gem and os.path.exists(ref):
        prompt = (f"Keep this presenter exactly as in the photo: same face, hair, glasses and red jacket. Place her waist-up, "
                  f"slightly left of centre, smiling at the camera with open welcoming hands, in front of a bright cinematic scene of "
                  f"{hero_hint or P['titleShort']} with a large white cruise ship on blue water behind her. Photorealistic, soft daylight, "
                  f"16:9. No text, no captions, no logo.")
        media.gemini_image(prompt, pres_scene, gem, ref_images=[ref])
    if not os.path.exists(pres_cut):
        gs = os.path.join(RONNIE, "green_screen.png")
        if os.path.exists(gs):
            media.chroma_cutout(gs, pres_cut)

    # the tour's own HLS intro
    intro = art("intro.mp4")
    if not os.path.exists(intro) and not args.no_intro:
        # skip the intro's own title card (the first ~15 s of every XO intro) - the hero carries its own title
        media.hls_clip(P.get("videoSrc"), intro, seconds=14, offset=args.intro_offset)
    # route map svg
    if P.get("routeSvg"):
        with open(art("route.svg"), "w", encoding="utf-8") as f:
            f.write(P["routeSvg"])

    P["media"] = dict(logo=rel(logo), qr=rel(qr) if qr and os.path.exists(qr) else None, cover=rel(cover),
                      posters=[rel(x) for x in posters if x], ships=ships,
                      presenterScene=rel(pres_scene) if os.path.exists(pres_scene) else None,
                      presenter=rel(pres_cut) if os.path.exists(pres_cut) else None,
                      intro=rel(intro) if os.path.exists(intro) else None,
                      introDur=round(media.ffprobe_duration(intro), 2) if os.path.exists(intro) else 0,
                      imagesFrom="gemini" if gem else "brochure/site", clipsFrom="pexels" if pex else "none")
    save_json(PLAN, P)
    log("media:", json.dumps({k: v for k, v in P["media"].items() if k not in ("ships",)}, ensure_ascii=False))

# ================================================================ voice
SCENES = [  # id, lead-in, tail (seconds of silence around the narration)
    ("hero", 1.0, 1.0), ("reasons1", 0.6, 0.8), ("reasons2", 0.5, 0.8), ("route", 0.6, 1.2),
    ("ports", 0.5, 0.8), ("ship", 0.5, 0.8), ("price", 0.6, 1.0), ("cta", 0.6, 3.5),
]

def voice_stage(args):
    P = load_json(PLAN)
    ref = os.path.join(RONNIE, "ronnie_ref.wav")
    nar = voicemod.Narrator(args.voice, os.path.join(VO, "cache"), ref_audio=ref, speed=args.speed, force=args.force)
    N = P["narration"]
    t = 0.0
    scenes, captions, audios, used = [], [], [], set()
    for sid, lead, tail in SCENES:
        lines = N.get(sid) or []
        if not lines:
            continue
        start = t; pcm = bytearray(); units = []; off = lead
        for line in lines:
            u, prov = nar.line(line); used.add(prov)
            dur = len(u) / 2 / voicemod.SR
            units.append(dict(zh=line, start=round(start + off, 3), end=round(start + off + dur, 3)))
            pcm += u; pcm += b"\x00" * int(GAP * voicemod.SR) * 2
            off += dur + GAP
        off -= GAP
        voicemod.write_wav(os.path.join(VO, f"{sid}.wav"), bytes(pcm))
        adur = len(pcm) / 2 / voicemod.SR
        dur = round(off + tail, 3)
        if sid == "ports":
            dur = max(dur, 3.2 * len(P["ports"]) + 0.5)
        if sid == "hero" and P.get("media", {}).get("introDur"):
            dur = max(dur, min(P["media"]["introDur"], 14) + 0.3)
        scenes.append(dict(id=sid, start=round(start, 3), dur=dur, narStart=round(start + lead, 3), units=units))
        audios.append(dict(id=sid, src=f"assets/vo/{sid}.wav", start=round(start + lead, 3), dur=round(adur, 3)))
        captions.extend(units)
        t = round(start + dur, 3)
        log(f"  {sid:9s} {start:6.2f} +{dur:5.2f}  narration {adur:5.2f}s")
    save_json(TIMING, dict(total=round(t, 3), scenes=scenes, captions=captions, audios=audios, voice=sorted(used)))
    log(f"TOTAL {t:.2f}s  voice={sorted(used)}")

# ================================================================ music
def music_stage(args):
    dst = os.path.join(ASSETS, "bgm")
    for ext in (".mp3", ".m4a", ".wav"):
        if os.path.exists(dst + ext):
            os.remove(dst + ext)
    got = None
    if args.youtube:
        log(f"music: yt-dlp {args.youtube}")
        got = media.youtube_audio(args.youtube, dst)
    if not got and args.bgm:
        src = args.bgm
        if src.startswith("http"):
            got = dst + os.path.splitext(src.split("?")[0])[1]; media.fetch(src, got)
        elif os.path.exists(src):
            got = dst + os.path.splitext(src)[1]; shutil.copy(src, got)
    credit_file = os.path.join(ASSETS, "bgm_credit.txt")
    if os.path.exists(credit_file):
        os.remove(credit_file)
    if got and args.bgm_credit:
        with open(credit_file, "w", encoding="utf-8") as f:
            f.write(args.bgm_credit.strip())
    log("music:", got or "none", "|", args.bgm_credit or "no credit line")

# ================================================================ compose
def compose(args):
    P = load_json(PLAN); T = load_json(TIMING)
    e = html.escape
    data = dict(P); data.pop("routeSvg", None); data.update(T)
    tpl = open(os.path.join(HERE, "tpl", "index.tpl.html"), encoding="utf-8").read()
    audios = "\n".join(f'      <audio id="vo-{a["id"]}" src="{a["src"]}" data-start="{a["start"]}" data-duration="{a["dur"]}" '
                       f'data-track-index="30" data-volume="1"></audio>' for a in T["audios"])
    videos = []
    S = {s["id"]: s for s in T["scenes"]}
    M = P.get("media", {})
    if M.get("intro") and "hero" in S:
        d = round(min(M["introDur"], S["hero"]["dur"]), 3)
        videos.append(f'      <video id="v-intro" class="clip" src="{M["intro"]}" muted playsinline preload="auto" '
                      f'data-start="{S["hero"]["start"]}" data-duration="{d}" data-track-index="5"></video>')
    if "ports" in S:
        units = S["ports"]["units"]
        for i, p in enumerate(P["ports"]):
            if p.get("clip") and i < len(units):
                st = units[i]["start"] - 0.3
                en = (units[i + 1]["start"] - 0.3) if i + 1 < len(units) else S["ports"]["start"] + S["ports"]["dur"]
                d = round(min(p["clipDur"], en - st), 3)
                videos.append(f'      <video id="v-port{p["day"]}" class="clip" src="{p["clip"]}" muted playsinline preload="auto" '
                              f'data-start="{round(st, 3)}" data-duration="{d}" data-track-index="6"></video>')
    svg = P.get("routeSvg") or ""
    svg = re.sub(r'<svg([^>]*)>', lambda m: '<svg' + re.sub(r'\s(width|height)="[^"]*"', '', m.group(1)) + ' id="route-svg">', svg, count=1)
    out = (tpl.replace("/*__DATA__*/", json.dumps(data, ensure_ascii=False))
              .replace("<!--__AUDIO__-->", audios).replace("<!--__VIDEO__-->", "\n".join(videos))
              .replace("<!--__ROUTE_SVG__-->", svg)
              .replace("__TOTAL__", str(T["total"])).replace("__TITLE__", e(P["titleShort"] + " · " + P["brandCn"])))
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)
    save_json(os.path.join(HERE, "meta.json"), {"id": f"promo-{P['tourId']}", "name": f"Promo {P['tourId']} · {P['titleShort']}"})
    log(f"wrote index.html  total {T['total']}s  videos {len(videos)}")

# ================================================================ finish
def bgm_credit():
    p = os.path.join(ASSETS, "bgm_credit.txt")
    return open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""

def youtube_metadata(P):
    lines = [f"# {P['titleFull']} | {P['brandCn']} {P['brandEn']}", "", "## Description", "",
             f"✨ {P.get('seoDesc') or P['titleFull']} ✨", ""]
    if P["ports"]:
        lines += ["🌟 行程亮點 (Tour Highlights)："] + [f"📍 {p['zh']}{(' ' + p['en']) if p['en'] else ''}" for p in P["ports"]] + [""]
    if P["reasons"]:
        lines += ["👑 必選理由："] + [f"{r['no']}. {r['title']}" for r in P["reasons"]] + [""]
    if P["includes"]:
        lines += ["✅ 團費包含：" + "、".join(P["includes"]), ""]
    if P.get("price"):
        lines += [f"💰 每位只要 US${P['price']} 起", ""]
    lines += ["📞 聯絡我們 (Contact Us)："] + [f"{p['country']} {p['phone']}" for p in P["phones"] if p["phone"]]
    lines += [f"🔗 行程詳情: {P['url']}", ""]
    if bgm_credit():
        lines += [f"🎵 Music: {bgm_credit()}", ""]
    lines += ["## Tags", "",
              ", ".join([x for x in [P.get("tag"), P.get("ship"), P["brandCn"], P["brandEn"]] if x] + [k.strip() for k in P.get("keywords", "").split(",") if k.strip()][:20])]
    return "\n".join(lines) + "\n"

def finish(args):
    P = load_json(PLAN); T = load_json(TIMING)
    tid = P["tourId"]; src = args.rendered
    final = os.path.join(OUT, f"promo_{tid}.mp4")
    bgm = next((os.path.join(ASSETS, "bgm" + x) for x in (".mp3", ".m4a", ".wav") if os.path.exists(os.path.join(ASSETS, "bgm" + x))), None)
    whoosh = os.path.join(RONNIE, "dragon-studio-whoosh-cinematic-376875.mp3")
    sting = os.path.join(RONNIE, "short_end.mp3")
    total = float(T["total"])
    inputs = ["-i", src]; filters = []; mix = ["[0:a]"]; n = 1
    if bgm:
        inputs += ["-stream_loop", "-1", "-i", bgm]
        filters.append(f"[{n}:a]volume={args.bgm_volume},atrim=0:{total:.3f},afade=t=in:st=0:d=2,afade=t=out:st={max(0, total - 3):.2f}:d=3[bg]")
        # duck the music under the narration
        filters.append("[bg][0:a]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=600:makeup=1[bgd]")
        mix.append("[bgd]"); n += 1
    S = {s["id"]: s for s in T["scenes"]}
    if os.path.exists(whoosh):
        cuts = [s["start"] for s in T["scenes"][1:]]
        for c in cuts[:8]:
            inputs += ["-i", whoosh]
            filters.append(f"[{n}:a]volume=0.5,adelay={int(max(0, c - 0.35) * 1000)}|{int(max(0, c - 0.35) * 1000)}[w{n}]")
            mix.append(f"[w{n}]"); n += 1
    if os.path.exists(sting) and "cta" in S:
        inputs += ["-i", sting]
        ms = int(S["cta"]["start"] * 1000)
        filters.append(f"[{n}:a]volume=0.7,adelay={ms}|{ms}[st]"); mix.append("[st]"); n += 1
    filters.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[a]")
    media.ffmpeg(*inputs, "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[a]",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", final)
    poster = os.path.join(OUT, f"promo_{tid}_poster.jpg")
    media.ffmpeg("-ss", "2.0", "-i", final, "-frames:v", "1", "-q:v", "2", poster)
    with open(os.path.join(OUT, f"youtube_metadata_{tid}.md"), "w", encoding="utf-8") as f:
        f.write(youtube_metadata(P))
    save_json(os.path.join(OUT, f"promo_{tid}_report.json"), dict(
        tourId=tid, seconds=total, voice=T.get("voice"), images=P.get("media", {}).get("imagesFrom"),
        clips=P.get("media", {}).get("clipsFrom"), music=os.path.basename(bgm) if bgm else None, musicCredit=bgm_credit() or None,
        presenterScene=bool(P.get("media", {}).get("presenterScene")), intro=bool(P.get("media", {}).get("intro"))))
    log("wrote", final, "|", poster)

# ================================================================ cli
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["plan", "media", "voice", "music", "compose", "finish", "all"])
    ap.add_argument("rendered", nargs="?", help="rendered mp4 (finish)")
    ap.add_argument("--id", required=True)
    ap.add_argument("--site", default="https://xotours.net")
    ap.add_argument("--images", default="auto", choices=["auto", "gemini", "brochure"])
    ap.add_argument("--clips", default="auto", choices=["auto", "pexels", "none"])
    ap.add_argument("--no-intro", action="store_true")
    ap.add_argument("--intro-offset", type=float, default=15.0, help="seconds into the HLS intro to start the hero clip")
    ap.add_argument("--voice", default="omnivoice", choices=["omnivoice", "edge"])
    ap.add_argument("--speed", type=float, default=1.05)
    ap.add_argument("--force", action="store_true", help="re-synthesize cached narration")
    ap.add_argument("--youtube", help="YouTube URL for the music bed (yt-dlp)")
    ap.add_argument("--bgm", help="music file or direct URL")
    ap.add_argument("--bgm-volume", default="0.22")
    ap.add_argument("--bgm-credit", default="", help="attribution line for a CC-BY track; goes into the YouTube description")
    args = ap.parse_args()
    stages = {"plan": plan, "media": media_stage, "voice": voice_stage, "music": music_stage, "compose": compose}
    if args.stage == "all":
        for s in ("plan", "media", "voice", "music", "compose"):
            stages[s](args)
    elif args.stage == "finish":
        if not args.rendered:
            sys.exit("finish needs the rendered mp4 path")
        finish(args)
    else:
        stages[args.stage](args)

if __name__ == "__main__":
    main()
