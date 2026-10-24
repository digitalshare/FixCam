"""Dev mode without a public URL: consume Chert's SSE event stream (like `stripe listen`).

    uv run python -m fixcam.listen
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import httpx

from . import channels, chert, config


def main() -> None:
    if not chert.enabled():
        raise SystemExit("Set CHERT_SIGNING_SECRET in .env first.")
    since = datetime.now(timezone.utc).isoformat()
    print(f"[listen] streaming Chert events since {since} — text your Chert number a video")
    while True:
        try:
            with httpx.stream("GET", f"{config.CHERT_BASE_URL}/api/v1/events/stream",
                              params={"since": since}, headers=chert._headers(json_body=False),
                              timeout=httpx.Timeout(10, read=90)) as r:
                r.raise_for_status()
                event_name = None
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line[5:].strip())
                        if event_name == "close":
                            since = payload.get("reconnect_with_since") or since
                        else:
                            since = payload.get("created_at") or since
                            envelope = payload.get("payload") or payload
                            threading.Thread(target=channels.handle_event, args=(envelope,), daemon=True).start()
                        event_name = None
        except httpx.HTTPError as e:
            print(f"[listen] stream error: {e}; reconnecting")
            time.sleep(2)


if __name__ == "__main__":
    main()
