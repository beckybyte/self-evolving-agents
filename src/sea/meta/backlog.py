"""Framework proposal backlog (docs/architecture_v2.md §8.2). One markdown file per proposal with a JSON header line,
kept under <state>/backlog/ (runtime state, like the inbox; default <repo>/runs/meta/backlog).

  python -m sea.meta.backlog list
  python -m sea.meta.backlog scan --run runs/brief_demo       # detectors + evolver wishes -> proposals
  python -m sea.meta.backlog add --title "..." --category search --evidence "..." --acceptance "..."
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path

CATEGORIES = ("expressiveness", "search", "learning", "evaluator", "infra")
STATUSES = ("open", "in_progress", "evaluating", "merged", "failed", "needs_human")
ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("SEA_META_STATE", ROOT / "runs" / "meta"))


class Backlog:
    def __init__(self, state: str | Path = STATE):
        self.dir = Path(state) / "backlog"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, pid: str) -> Path:
        return self.dir / f"{pid}.md"

    def get(self, pid: str) -> dict:
        head, _, body = self._path(pid).read_text().partition("\n")
        return {**json.loads(head.removeprefix("<!-- ").removesuffix(" -->")), "body": body}

    def save(self, p: dict):
        head = {k: v for k, v in p.items() if k != "body"}
        self._path(p["id"]).write_text(f"<!-- {json.dumps(head, ensure_ascii=False)} -->\n{p['body']}")

    def all(self) -> list[dict]:
        return sorted((self.get(f.stem) for f in self.dir.glob("P*.md")), key=lambda p: p["id"])

    def add(self, title: str, category: str, evidence: str, acceptance: str = "", direction: str = "",
            source: str = "human", key: str | None = None, impact: int = 1, expected_prompt_changes: str = "none") -> dict:
        assert category in CATEGORIES, f"category must be one of {CATEGORIES}"
        key = key or re.sub(r"\W+", "-", title.lower())[:60]
        for p in self.all():
            if p["key"] == key and p["status"] not in ("merged",):
                p["count"] += 1
                p["body"] += f"\n### more evidence ({source}, {time.strftime('%Y-%m-%d %H:%M')})\n{evidence}\n"
                self.save(p)
                return p
        pid = f"P{len(list(self.dir.glob('P*.md'))) + 1:03d}"
        p = {"id": pid, "title": title, "category": category, "status": "open", "key": key, "count": 1, "impact": impact,
             "attempts": 0, "source": source, "created": time.strftime("%Y-%m-%d %H:%M"),
             "expected_prompt_changes": expected_prompt_changes,
             "body": (f"# {pid}: {title}\n\ncategory: {category} · source: {source}\n\n## Problem and evidence\n{evidence}\n\n"
                      f"## Suggested direction\n{direction or '(open)'}\n\n## Acceptance\n{acceptance or '(tiers A-C pass)'}\n")}
        self.save(p)
        return p

    def set_status(self, pid: str, status: str, note: str = ""):
        assert status in STATUSES
        p = self.get(pid)
        p["status"] = status
        if note:
            p["body"] += f"\n### {status} ({time.strftime('%Y-%m-%d %H:%M')})\n{note}\n"
        self.save(p)


# ------------------------------------------------------------------ detectors (pure code, §8.2)
def detect(run_dir: str | Path) -> list[dict]:
    """Scan one evolution run for framework-level symptoms. Returns proposal kwargs for Backlog.add."""
    run = Path(run_dir)
    out = []
    hist = [json.loads(l) for l in open(run / "history.jsonl")] if (run / "history.jsonl").exists() else []
    inval = [h["error"] for h in hist if h.get("event") == "invalid_genome"]
    for msg, n in Counter(re.sub(r"\d+|'[^']*'", "_", e)[:120] for e in inval).items():
        if n >= 3:
            out.append(dict(title=f"Evolver keeps writing genomes the framework rejects: {msg}", category="expressiveness",
                            evidence=f"{n} invalid children in {run} with: {msg}", source="detector:invalid_genome",
                            key=f"invalid:{msg[:50]}", impact=n))
    for rows_f in run.glob("evals/*/*/fit/rows.jsonl"):
        rows = [json.loads(l) for l in open(rows_f)]
        if len(rows) >= 3 and len({r["score"] for r in rows}) == 1 and not rows[0].get("reason") == "missing_artifact":
            out.append(dict(title="Judge gives the same score to every scenario (no discrimination)", category="evaluator",
                            evidence=f"{rows_f}: {len(rows)} scenarios all scored {rows[0]['score']}",
                            source="detector:judge_flat", key="judge-flat", impact=2))
    st = json.loads((run / "state.json").read_text()) if (run / "state.json").exists() else {}
    ll = st.get("learn_log", [])
    if len(ll) >= 3 and not any(x.get("accepted") for x in ll[-3:]):
        out.append(dict(title="Gated learning rejects every memory update", category="learning",
                        evidence=f"{run}: last {min(len(ll), 3)} learning rounds all rejected by the holdout gate",
                        source="detector:learn_gate", key="learn-gate-rejects", impact=1))
    by_att: dict = {}
    for h in hist:
        if h.get("attempt"):
            by_att.setdefault((h["gen"], h["attempt"]), []).append(h)
    radical_lost = [k for k, hs in by_att.items() if any(h.get("radical") for h in hs)
                    and not any(h.get("radical") and h.get("new_cell") for h in hs)]
    if len(radical_lost) >= 3:
        out.append(dict(title="Radical children never open a new archive niche", category="search",
                        evidence=f"{run}: radical child failed to enter the archive in attempts {radical_lost}",
                        source="detector:radical", key="radical-never-survives", impact=1))
    collapsed = [k for k, hs in by_att.items() if len(hs) >= 2 and len({tuple(h.get("roles", [])) for h in hs}) == 1]
    if len(collapsed) >= 2:
        out.append(dict(title="Children of one attempt are structurally identical (search collapse)", category="search",
                        evidence=f"{run}: identical role lists in attempts {collapsed}", source="detector:collapse",
                        key="search-collapse", impact=1))
    for w in sorted(run.glob("workspaces/*/FRAMEWORK_WISHES.md")):
        text = w.read_text().strip()
        if text:
            out.append(dict(title=f"Evolver wish ({w.parent.name}): {text.splitlines()[0][:90]}", category="expressiveness",
                            evidence=f"{w}:\n{text[:2000]}", source="evolver_wish", key=f"wish:{run.name}:{w.parent.name}",
                            impact=1))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "scan", "add", "show"])
    ap.add_argument("--run")
    ap.add_argument("--id")
    ap.add_argument("--title")
    ap.add_argument("--category")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--acceptance", default="")
    ap.add_argument("--direction", default="")
    ap.add_argument("--prompt-changes", default="none", choices=["none", "brief", "any"])
    a = ap.parse_args()
    b = Backlog()
    if a.cmd == "list":
        for p in sorted(b.all(), key=lambda p: (p["status"] != "open", -p["count"] * p["impact"])):
            print(f"{p['id']} [{p['status']}] ({p['category']}, x{p['count']}) {p['title']}")
    elif a.cmd == "show":
        print(b.get(a.id)["body"])
    elif a.cmd == "scan":
        for kw in detect(a.run):
            p = b.add(**kw)
            print(p["id"], p["title"])
    else:
        p = b.add(a.title, a.category, a.evidence, a.acceptance, a.direction, expected_prompt_changes=a.prompt_changes)
        print(p["id"])
