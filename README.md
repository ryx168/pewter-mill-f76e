# Tour promo video, rendered on GitHub Actions

A tour's promotion video, built unattended on a GitHub-hosted runner from the tour's
public data: the tour JSON, the live brochure page, the tour's own intro clip, the brochure's
palette. One workflow, one MP4 per run.

```
.github/workflows/promo.yml   the workflow (Run workflow, or push promo_video/run.json)
promo_video/                  the engine: build.py plan|media|voice|music|compose|finish
promo_video/tpl/              HyperFrames composition template (brochure look)
assets/ronnie/                presenter stills, stingers, and the ENCRYPTED voice reference
includes/logo_gold.png        logo
kv_json_exports/tour_<id>.json  tour data (absent ids are fetched from the live site)
voice_tool.py                 encrypt / decrypt the voice reference
```

## Run it

**Actions → Tour promo video → Run workflow**, pick the tour id, voice, quality, an optional
YouTube URL for the music bed. Or edit `promo_video/run.json` and push.

The MP4, poster, YouTube metadata and a review contact sheet land in the run's artifact
(`promo-<id>`) and in a Release tagged `promo-<id>-run<n>`. A one-file report is committed
back to `promo_video/runs/`.

| step | source | fallback |
|---|---|---|
| port images | Gemini `gemini-2.5-flash-image` (`GEMINI_API_KEY`) | brochure photos → day images |
| clips | Pexels (`PEXELS_API_KEY`) | Ken Burns on the stills |
| narration | OmniVoice clone of the presenter (`VOICE_KEY`) | edge-tts `zh-TW-HsiaoChenNeural` |
| music | yt-dlp from a YouTube URL (GitHub IPs are often refused) | direct file URL → none |

With **no secrets at all** the workflow still produces a complete video: brochure photos,
Ken Burns, edge-tts narration, no music bed.

## Secrets (all optional)

| secret | effect |
|---|---|
| `VOICE_KEY` | passphrase for `assets/ronnie/ronnie_ref.wav.enc`; enables `voice: omnivoice` |
| `GEMINI_API_KEY` | AI port images and presenter scene (needs prepaid credit; 402 → brochure fallback) |
| `PEXELS_API_KEY` | stock clips per port |
| `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | `publish_r2` upload |

## The voice

The presenter's reference recording is a real person's voice, so it is only committed
**encrypted** (`assets/ronnie/ronnie_ref.wav.enc`, `openssl enc -aes-256-cbc -pbkdf2`), with
its transcript beside it (`ronnie_ref.txt`, so the runner never downloads Whisper). Without
`VOICE_KEY` the workflow uses edge-tts; with it, `voice: omnivoice` clones the presenter on
CPU (slow: ~60x real time at 32 steps, the workflow uses 16; expect an hour or more per video).

```
python voice_tool.py encrypt --key-file /path/to/key.txt --src /path/to/new_ref.wav
```

## Locally

```
cd promo_video
python build.py --id 7622 all --voice edge --youtube "https://www.youtube.com/watch?v=..."
npx --yes hyperframes@0.5.1 render -q standard -o renders/promo_7622.mp4
python build.py --id 7622 finish renders/promo_7622.mp4
```

See `promo_video/README.md` for the engine's details and what was measured.
