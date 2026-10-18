"""Screenshot UI judge: seed neutral demo data, take 2 headless-Chrome screenshots, ask Claude to score a rubric.

python builds/uijudge/judge.py <app_dir> --out <dir> [--repeats 2] [--model sonnet]
score = mean over criteria of (score_1to5 - 1) / 4. Separate metric; not part of fitness.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "usersim"))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import personas  # noqa: E402
import sim  # noqa: E402
from run import App  # noqa: E402

RUBRIC = {
    "a_hierarchy_chat": "clear visual hierarchy, with the chat as the main control (talk, don't form-fill)",
    "b_focus_card": "the focus card is prominent and shows only the next tiny step, its why, and a Start 5 minutes "
                    "button",
    "c_calm_style": "calm visual style: no red alarms or walls of overdue items; soft time blocks or windows",
    "d_proposal_card": "a proposal card is clearly separate from the rest, with Try it / Not now",
    "e_timeline_glance": "the calendar or timeline is readable at a glance",
    "f_operable": "the page is operable without guessing: obvious inputs and buttons, a sensible layout that uses the "
                  "width (not one long column), nothing broken, cut off or overlapping",
    "g_learned_activity": "the learned panel and the activity feed are discoverable but not dominant",
}


def seed(base: str) -> dict:
    """Neutral demo data: evening done-history, a 4-fragment dump, a vague goal broken down and accepted, /api/learn."""
    now = sim.BASE_DAY.replace(hour=19)
    p = dict(personas.IRIS, open=[])
    log = {"reset": sim.http(base, "POST", "/api/reset", {"now": sim.fmt(now), "items": sim.seed_items(p, now)})
           .get("status")}
    r = sim.http(base, "POST", "/api/command", {"text": "call mom, portfolio, maybe yoga?, dentist thursday 3pm",
                                                "mode": "dump"})
    log["dump"] = r.get("status")
    r = sim.http(base, "POST", "/api/command", {"text": "get my portfolio together", "mode": "command"})
    js = r.get("json") or {}
    goal = next((m.get("item_id") for m in js.get("messages") or [] if m.get("kind") == "question"), None) or \
        (js.get("created") or [None])[0]
    answers = ["yes", "I want a job where I can be creative", "a simple page with three projects",
               "by the end of November"]
    accepted = False
    for k in range(8):
        st = sim.http(base, "GET", "/api/state").get("json") or {}
        last = next((m for m in reversed(st.get("messages") or []) if m.get("role") == "agent"
                     and m.get("item_id") == goal), None)
        if last and last.get("kind") == "breakdown":
            text, accepted = "looks good", True
        else:
            text = answers[min(k, len(answers) - 1)]
        sim.http(base, "POST", "/api/chat", {"item_id": goal, "text": text})
        if accepted:
            break
    log["breakdown_accepted"] = accepted
    r = sim.http(base, "POST", "/api/learn", {})
    log["proposal"] = bool((r.get("json") or {}).get("proposal") or
                           (sim.http(base, "GET", "/api/proposal").get("json") or {}).get("proposal"))
    return log


def screenshot(url: str, path: Path):
    subprocess.run(["google-chrome", "--headless=new", "--no-sandbox", "--hide-scrollbars", "--disable-gpu",
                    "--virtual-time-budget=8000", "--window-size=1400,1000", f"--screenshot={path}", url],
                   capture_output=True, timeout=90)
    if not path.exists():
        raise RuntimeError(f"screenshot failed: {path}")


def ask_claude(out_dir: Path, shots: list, model: str) -> tuple[dict, float, str]:
    crit = "\n".join(f'- "{k}": {v}' for k, v in RUBRIC.items())
    prompt = (
        "You are judging the user interface of a gentle conversational calendar app for a person who finds planning "
        "hard. Use the Read tool to look at these two screenshots in the current directory: "
        + ", ".join(s.name for s in shots) +
        " (the first is right after some demo data was added; the second is the same page 2 simulated hours later).\n"
        "Score each criterion from 1 (bad) to 5 (excellent), judging only what is visible:\n" + crit +
        "\n\nReply with ONLY a JSON object, no other text: {\"criteria\": {\"<key>\": <1-5>, ...}, "
        "\"notes\": \"<2-4 sentences: the biggest strengths and problems>\"}")
    cmd = ["claude", "-p", "--model", model, "--allowedTools", "Read", "--output-format", "json",
           "--no-session-persistence", "--setting-sources", "project", "--strict-mcp-config",
           "--disable-slash-commands"]
    p = subprocess.run(cmd, input=prompt, cwd=out_dir, capture_output=True, text=True, timeout=600)
    d = json.loads(p.stdout)
    text = d.get("result", "")
    m = re.search(r"\{.*\}", text, re.S)
    res = json.loads(m.group(0)) if m else {}
    return res, float(d.get("total_cost_usd") or 0.0), text


def judge(app_dir: str, out_dir: str, model: str = "sonnet") -> dict:
    t0 = time.time()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    app = App(app_dir)
    try:
        seeded = seed(app.base)
        shots = [out / "screen_1_fresh.png", out / "screen_2_plus2h.png"]
        screenshot(app.base + "/", shots[0])
        st = sim.http(app.base, "GET", "/api/state").get("json") or {}
        now = sim.parse(st.get("now")) or sim.BASE_DAY.replace(hour=19)
        sim.http(app.base, "POST", "/api/clock", {"now": sim.fmt(now + timedelta(hours=2))})
        screenshot(app.base + "/", shots[1])
    finally:
        app.stop()
    res, cost, raw = ask_claude(out, shots, model)
    crit = {k: int(v) for k, v in (res.get("criteria") or {}).items() if k in RUBRIC and str(v).isdigit()}
    score = round(sum((v - 1) / 4 for v in crit.values()) / len(crit), 4) if crit else None
    r = {"app_dir": app_dir, "score": score, "criteria": crit, "notes": res.get("notes", "" if crit else raw[:500]),
         "screens": [str(s) for s in shots], "cost_usd": round(cost, 4), "seeded": seeded,
         "secs": round(time.time() - t0, 1), "missing": [k for k in RUBRIC if k not in crit]}
    (out / "judge.json").write_text(json.dumps(r, indent=1))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    rs = []
    for i in range(a.repeats):
        r = judge(a.app_dir, str(Path(a.out) / f"rep{i}") if a.repeats > 1 else a.out, a.model)
        rs.append(r)
        print(f"rep {i}: score {r['score']}  {r['criteria']}  ${r['cost_usd']}  {r['secs']}s  seeded {r['seeded']}")
        print(f"  notes: {r['notes']}", flush=True)
    sc = [r["score"] for r in rs if r["score"] is not None]
    if sc:
        print(f"MEAN {sum(sc) / len(sc):.3f}  range {min(sc):.3f}-{max(sc):.3f}  "
              f"cost ${sum(r['cost_usd'] for r in rs):.3f}  time {sum(r['secs'] for r in rs):.0f}s")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "summary.json").write_text(json.dumps(rs, indent=1))


if __name__ == "__main__":
    main()
