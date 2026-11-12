# Tour promotion video — the 7622 pipeline, automated

The 7622 (盛世公主號 愛爾蘭與英倫) video in the hand-made 7622 video was produced by hand:
Gemini images and clips, Google Vids stock/effect footage, a Movavi slideshow, Ronnie's
cloned voice from OmniVoice, YouTube music, the brochure PDF's look. This folder does the
same steps from a tour's PUBLIC data, so it runs unattended on a GitHub runner
(`.github/workflows/promo.yml`) or on this PC.

| step | what | source | fallback |
|---|---|---|---|
| plan | shots, 必選理由, route map, narration | `kv_json_exports/tour_<id>.json` + the live brochure page (`XO_BROCHURE_PAGES` on the tour page) | tour JSON only |
| media | port images | Gemini `gemini-2.5-flash-image` (`GEMINI_API_KEY`) | brochure photos → day images |
| | presenter | Gemini scene with Ronnie's headshot as reference | chroma-key cutout of `assets/ronnie/green_screen.png` |
| | clips | Pexels (`PEXELS_API_KEY`) | Ken Burns on the stills |
| | intro | the tour's HLS cover video (videos.xotours.ca), 14 s, muted | cover image |
| voice | narration | OmniVoice clone of `assets/ronnie/ronnie_ref.wav` | edge-tts `zh-TW-HsiaoChenNeural` |
| music | bed | yt-dlp from a YouTube URL | a direct file URL → none |
| compose | HyperFrames page in the brochure palette (`tpl/index.tpl.html`) | | |
| finish | music ducked under speech, whoosh at cuts, end sting, loudnorm, poster, YouTube metadata | `assets/ronnie/*.mp3` | |

```
cd tour/promo_video
python build.py --id 7622 all --voice edge --youtube "https://www.youtube.com/watch?v=OIb7zfhTuvM"
npx --yes hyperframes@0.5.1 render -q standard -o renders/promo_7622.mp4
python build.py --id 7622 finish renders/promo_7622.mp4      # -> out/promo_7622.mp4 (+ poster, metadata, report)
python review_frames.py renders/promo_7622.mp4               # one frame per key moment -> renders/review/
```

Each stage is cached under `assets/` (images, TTS lines by text hash, the intro clip) so a
re-run after a copy change only redoes what changed. Delete `assets/` for a clean build.

## What was measured (2026-11-11)

- **Gemini**: `GEMINI_API_KEY` in `.env` is valid (63 models, incl. `gemini-2.5-flash-image`)
  but answers `402 prepayment credits are depleted`. No Veo model is on this key. Until the
  AI Studio project is topped up, images come from the brochure and the presenter is the
  green-screen cutout; the code path is written and falls back on the first 402.
- **OmniVoice on CPU is too slow for a runner**: with the reference encoded once, one 3.8 s
  line took 225 s at 32 steps on this 20-core PC (60x real time). A two-minute narration is
  ~2 h here and would exceed a 4-core runner's job limit. `--voice omnivoice` is kept for a
  GPU box (`OMNIVOICE_STEPS` lowers the diffusion steps); the workflow default is edge-tts.
- **yt-dlp** works from this PC; GitHub's datacenter IPs are often refused by YouTube, so the
  workflow treats the music step as optional and accepts a direct file URL instead. The
  7622 tracks (Auld Lang Syne, Pomp and Circumstance, Riverdance, London Bridge, Big Ben)
  are copyrighted recordings - fine for a preview, Content ID will claim them on YouTube.
- Brochure assets resolve on `https://xotours.net/assets/pdfs/<id>/brochure/*.jpg`; the
  hand-named pages (7617/7620/7622) are found through the tour page's `XO_BROCHURE_PAGES` map.

## Secrets / variables the workflow reads

`GEMINI_API_KEY`, `PEXELS_API_KEY` (optional), `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`
(only for the optional R2 publish to `xotours-videos/tour-intros/<id>/promo/`).
