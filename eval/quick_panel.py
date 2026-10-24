"""Fast persona panel: MatrAIx persona profiles chat with FixCam, then self-report.

Same self-report fields as MatrAIx's chat tasks, but runs in-process (no Docker) so you
can iterate on prompts in ~1 minute. Use the real MatrAIx harness for final numbers.

    uv run python eval/quick_panel.py --personas /path/to/MatrAIx-Persona-8B/persona/datasets/matraix-persona-dev-sample \
        --case washer-4c --n 6 --label baseline
    uv run python eval/quick_panel.py --compare        # table of all past runs
"""

from __future__ import annotations

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
import httpx
import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "evals"
PERSONA_MODEL = "claude-haiku-5-5"  # cheap simulated users; FixCam itself runs on its own model
client = anthropic.Anthropic()


class Turn(BaseModel):
    message: str = Field(description="Your next text message to FixCam, in your own voice")
    done: bool = Field(description="True if you have what you need or have given up")


class SelfReport(BaseModel):
    needConstraintSatisfaction: str = Field(description="yes | partially | no")
    personalPreferenceSatisfaction: str = Field(description="yes | partially | no")
    overallExperienceRating: int = Field(description="1-10")
    reason: str
    couldDoItYourself: bool = Field(description="Could you realistically carry out the fix yourself?")
    confusingParts: list[str]


def persona_prompt(persona: dict, case_md: str) -> str:
    dims = "\n".join(f"- {k}: {v}" for k, v in (persona.get("dimensions") or {}).items())
    return (f"You are role-playing a real person named {persona.get('display_name', 'the user')}.\n"
            f"Your traits:\n{dims}\n\nSituation:\n{case_md}\n\n"
            "You are texting FixCam (a repair assistant) from your phone. Write like this person "
            "actually texts: length, vocabulary, patience, and tech-savviness should follow the traits. "
            "Do not be artificially agreeable.")


def simulate(persona_path: Path, case: str, base_url: str, max_turns: int) -> dict:
    persona = yaml.safe_load(persona_path.read_text())
    system = persona_prompt(persona, (ROOT / "samples" / case / "case.md").read_text())
    session_id = f"panel-{case}-{persona.get('persona_id')}-{int(time.time())}"
    transcript: list[dict] = []
    for i in range(max_turns):
        history = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in transcript) or "(no messages yet)"
        ask = ("You just sent FixCam your video. Write your first message." if i == 0 else
               "Conversation so far:\n" + history + "\n\nWrite your next message (or set done=true).")
        turn = client.messages.parse(model=PERSONA_MODEL, max_tokens=2000, system=system,
                                     messages=[{"role": "user", "content": ask}], output_format=Turn).parsed_output
        if turn is None or (turn.done and i > 0):
            break
        transcript.append({"role": "you", "content": turn.message})
        r = httpx.post(f"{base_url}/v1/messages", timeout=300,
                       json={"message": turn.message, "sessionId": session_id, "caseId": case})
        r.raise_for_status()
        transcript.append({"role": "fixcam", "content": r.json()["reply"]})
    history = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in transcript)
    report = client.messages.parse(
        model=PERSONA_MODEL, max_tokens=2000, system=system, output_format=SelfReport,
        messages=[{"role": "user", "content": f"Your chat with FixCam:\n{history}\n\n"
                   "Reflect honestly, as yourself, on how useful and clear FixCam was."}]).parsed_output
    return {"persona": persona.get("persona_id"), "name": persona.get("display_name"),
            "report": report.model_dump() if report else None, "transcript": transcript}


def summarize(run: dict) -> dict:
    reports = [r["report"] for r in run["results"] if r.get("report")]
    n = len(reports) or 1
    return {
        "label": run["label"], "case": run["case"], "n": len(reports),
        "avg_rating": round(sum(r["overallExperienceRating"] for r in reports) / n, 2),
        "need_met_%": round(100 * sum(r["needConstraintSatisfaction"] == "yes" for r in reports) / n),
        "diy_%": round(100 * sum(r["couldDoItYourself"] for r in reports) / n),
    }


def compare() -> None:
    rows = [summarize(json.loads(p.read_text())) for p in sorted(RESULTS.glob("*.json"))]
    print(f"{'label':<20}{'case':<14}{'n':>3}{'rating':>8}{'need met':>10}{'DIY':>6}")
    for r in rows:
        print(f"{r['label']:<20}{r['case']:<14}{r['n']:>3}{r['avg_rating']:>8}{r['need_met_%']:>9}%{r['diy_%']:>5}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", type=Path, help="Directory of MatrAIx persona_*.yaml files")
    ap.add_argument("--case", default="washer-4c")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--label", default="run")
    ap.add_argument("--seed", type=int, default=7, help="Same seed = same personas, for fair before/after")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()
    if args.compare:
        return compare()
    if not args.personas:
        ap.error("--personas is required")

    pool = sorted(args.personas.glob("persona_*.yaml"))
    picked = random.Random(args.seed).sample(pool, min(args.n, len(pool)))
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda p: simulate(p, args.case, args.base_url, args.turns), picked))
    run = {"label": args.label, "case": args.case, "ts": int(time.time()), "results": results}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{run['ts']}_{args.label}.json").write_text(json.dumps(run, indent=2))
    for r in results:
        rep = r["report"] or {}
        print(f"- {r['name']:<22} {rep.get('overallExperienceRating', '?'):>2}/10  {rep.get('reason', '')[:90]}")
    print(summarize(run))


if __name__ == "__main__":
    main()
