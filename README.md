# FixCam: text a video, get it fixed

Text FixCam an iMessage video of something broken. It watches the video, reads the error code, looks up the fix on the web, and replies with step-by-step instructions **drawn onto frames of your own video**, plus a parts link and a voice note.

```
iMessage video ──► Chert webhook ──► ffmpeg (key frames + audio) ──► STT
                                         │
                       Claude (vision): diagnose device / error / search queries
                                         │
                       Keenable: search + extract repair steps from top pages
                                         │
                       Claude: grounded fix plan + where to point on each frame
                                         │
                PIL callouts on your frames ─► Chert: text + images + link + voice note
                                         │
                MatrAIx persona panel grades it (grandma, engineer, non-native speaker…)
```

| Sponsor | Role |
|---|---|
| **Chert** | iMessage in/out: video download, image upload, rich links, typing dots, native voice notes |
| **Keenable** | Web search + page extraction for the official fix (`fixcam/keenable.py`) |
| **MatrAIx** | Simulated users with persona profiles rate FixCam (`eval/`) |

## Run

```bash
cp .env.example .env          # fill in keys
uv sync
uv run uvicorn fixcam.server:app --port 8000
```

- **Browser demo / backup** (no Chert needed): open http://localhost:8000. Attach a video, then send. Text `reset` to start over.
- **iMessage via webhook**: expose the port (`cloudflared tunnel --url http://localhost:8000`) and create a Chert subscription to `https://<tunnel>/webhooks/chert` with `events: ["message.received"]`. Put the returned secret in `CHERT_WEBHOOK_SECRET`.
- **iMessage without a public URL**: `uv run python -m fixcam.listen` (reads Chert's SSE event stream).

## Evaluate with MatrAIx personas

Get the persona profiles first: `git clone https://github.com/MatrAIx-ai/MatrAIx-Persona-8B`.

**Quick panel** (about 1 min, no Docker; good for before/after comparisons while tuning prompts):
```bash
uv run python eval/quick_panel.py --personas ../MatrAIx-Persona-8B/persona/datasets/matraix-persona-dev-sample \
    --case washer-4c --n 6 --label baseline
# ...change a prompt in fixcam/agent.py, restart the server...
uv run python eval/quick_panel.py --personas ... --case washer-4c --n 6 --label v2
uv run python eval/quick_panel.py --compare
```

**Full MatrAIx harness** (Playground / `matraix run`, with real persona agents in Docker):
```bash
bash eval/matraix/install.sh ../MatrAIx-Persona-8B      # one chat task per samples/<case>/
cd ../MatrAIx-Persona-8B
uv run python application/scripts/generate_application_job.py \
  --task application/tasks/fixcam-chat_washer-4c --execution-mode auto --sample-size 6
uv run matraix run -c configs/jobs/application-task-job-recipe/<generated>.yaml
```
A sidecar inside the trial network forwards persona messages to FixCam on your host (`host.docker.internal:8000`). It also tells FixCam which sample video that persona "sent".

## Add a demo case

`samples/<case-id>/` contains a video or photos and a `case.md`, which describes the situation from the user's point of view. The `washer-4c` clip is synthetic, only for smoke tests. **Record real phone videos for the demo.**

## Layout

| Path | What |
|---|---|
| `fixcam/agent.py` | Pipeline: ingest → diagnose → research → plan → render; follow-up chat |
| `fixcam/chert.py` | Chert API client + webhook signature check |
| `fixcam/keenable.py` | Search/fetch with an automatic keyless fallback |
| `fixcam/media.py`, `annotate.py`, `speech.py` | ffmpeg, image callouts, STT/TTS |
| `fixcam/server.py` | `/webhooks/chert`, MatrAIx `/v1/messages`, demo page |
| `eval/` | Quick persona panel + MatrAIx task installer |
