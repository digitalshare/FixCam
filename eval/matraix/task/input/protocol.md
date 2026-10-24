# FixCam chat API protocol

FixCam is a repair assistant you normally text over iMessage. For this session it is
reachable as a **REST API** on the compose sidecar `fixcam-api`
(`http://fixcam-api:8000` from this container). Your phone video of the problem has
**already been sent** — FixCam can see it. Its replies may mention marked-up photos
of your video (shown as `[Photo N: ...]`) and links.

## Endpoints

| Method | Path | Body | Response |
|--------|------|------|----------|
| `POST` | `/v1/messages` | `{"message": "<your text>"}` | `{"reply": "<FixCam reply>"}` |
| `GET` | `/v1/conversation` | — | `{"messages": [{"role": "customer"|"support", "content": "..."}, ...]}` |

Replies can take 20–60 seconds while FixCam watches the video and searches the web —
use a long timeout (e.g. `curl --max-time 180`).

1. `POST` to `/v1/messages` at least twice as yourself.
2. Follow (or imagine following) the steps and ask about anything unclear.
3. Continue until you can judge whether FixCam gave you a fix you could actually carry out.
