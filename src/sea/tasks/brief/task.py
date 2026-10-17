"""BriefTask: an open-ended product brief, no dataset. The evaluation is built by the Framer (goal/v<N>/), applied by the
Judge, and revised when the human answers (refresh). One evaluation = the team runs ONCE on the brief and the judge
scores its artifacts per scenario (fit or holdout) plus once globally.

Fitness = score(fit) - lam_tokens * team_tokens/1000 - lam_complexity * complexity_points/100
score(split) = sum_c w_c * (mean judge score of c - 1)/4, times (#required artifacts present / #required).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
from pathlib import Path

from ...executor import run_team
from ...fitness import FitnessSpec, Term
from ...human import HumanChannel
from ..base import Task, summarize
from . import framer, judge

REQUIRED_ARTIFACTS = ("prd", "decisions")
TASTE_MAX_BULLETS, TASTE_MAX_COMMENTS, TASTE_MAX_CHARS, TASTE_MAX_BULLET_CHARS = 12, 4, 1200, 200
DESC = """TASK the team solves: design a product from a vague brief. There is NO dataset and NO ground truth. The team
runs once per evaluation on the brief and must produce design documents, emitted by any llm role as
<artifact name="prd">...</artifact> and <artifact name="decisions">...</artifact> (more artifacts, e.g. "flows",
are welcome). `decisions` is the trade-off log: one entry per feature/question with options considered, the choice,
the reason, and the assumption ids (A1, Q2, ...) it depends on.
Context selectors available to roles: input (= the brief), brief, restatement, assumptions, human_answers (the
team's taste: the human's answers and rating comments, capped; every llm role that does not read it gets it in its
system message instead), open_questions. Roles may ask the human with a line `ASK_HUMAN: <question> | DEFAULT: <best guess>`; the run never
waits, it proceeds on the default (recorded as an assumption); answers arrive in later generations.

A judge scores the documents with the rubric (rubric.json), walking through scenarios as personas. You see the
fit scenarios and the judge's critiques (critiques.jsonl); further HIDDEN scenarios with other personas are
used for gating, so design for the personas in general, not for the visible scenarios one by one.
Complexity: the judge sizes every committed first-release feature (S=1, M=3, L=8 points); points cost fitness.

THE BRIEF:
{brief}

OUR CURRENT UNDERSTANDING (restatement, version {ver}):
{restatement}"""


class BriefTask(Task):
    name = "brief"
    context_keys = ("brief", "restatement", "assumptions", "human_answers", "open_questions")

    def __init__(self, brief_path: str, run_dir: str | Path, human: HumanChannel, lam_tokens: float = 0.002,
                 lam_complexity: float = 0.1, framer_model: str = "claude:sonnet", judge_model: str = "qwen2.5-72b",
                 framer: str | None = None, judge: str | None = None, judge_workers: int = 6):
        super().__init__({"fit": 1.0}, "holdout", [], "fit", lam_tokens, human)
        self.brief = Path(brief_path).read_text()
        self.run_dir = Path(run_dir)
        self.goal_dir = self.run_dir / "goal"
        self.framer_model, self.judge_model = framer or framer_model, judge or judge_model
        self.lam_complexity, self.judge_workers = lam_complexity, judge_workers
        self.ensure_goal()

    # ---------------------------------------------------------------- goal / evaluation versions
    def version(self) -> str:
        return (self.goal_dir / "CURRENT").read_text().strip()

    def goal(self) -> dict:
        return json.loads((self.goal_dir / self.version() / "goal.json").read_text())

    def ensure_goal(self):
        if (self.goal_dir / "CURRENT").exists():
            return
        d, u = framer.frame(self.brief, self.framer_model)
        framer.write_version(self.goal_dir, "v1", d)
        self._post_questions(d, "v1")
        self._log_usage("frame v1", u)

    def _post_questions(self, d: dict, ver: str):
        for q in d.get("questions", [])[:4]:
            self.human.ask(q["question"], q.get("default", ""), asker="framer", tag=f"{ver}:{q.get('assumption', '')}",
                           context=q.get("why", ""))

    def _log_usage(self, what: str, u: dict):
        with open(self.goal_dir / "framer_usage.jsonl", "a") as f:
            f.write(json.dumps({"what": what, **u, "ts": time.strftime("%H:%M:%S")}) + "\n")

    def _new_human_input(self) -> tuple[str, dict]:
        ff = self.goal_dir / "folded.json"
        folded = json.loads(ff.read_text()) if ff.exists() else {"qids": [], "ratings": 0}
        new_q = [p for p in self.human.answered_pairs() if p["qid"] not in folded["qids"]]
        new_r = self.human.ratings()[folded["ratings"]:]
        lines = [f"- {p['qid']} (asked by {p['asker']}): {p['question']}\n  we had assumed: {p['default']}\n  HUMAN ANSWER: {p['answer']}"
                 for p in new_q]
        lines += [f"- rating of design {r['genome_id']}: {r['score']}/5. comment: {r['comment']}" for r in new_r]
        state = {"qids": folded["qids"] + [p["qid"] for p in new_q], "ratings": folded["ratings"] + len(new_r)}
        return "\n".join(lines), state

    def refresh(self) -> bool:
        text, state = self._new_human_input()
        if not text:
            return False
        cur = self.version()
        nxt = f"v{int(cur[1:]) + 1}"
        d, u = framer.revise(self.brief, self.goal(), cur, text, self.framer_model)
        framer.write_version(self.goal_dir, nxt, d)
        (self.goal_dir / nxt / "human_input.md").write_text(text)
        (self.goal_dir / "folded.json").write_text(json.dumps(state))
        self._post_questions(d, nxt)
        self._log_usage(f"revise {cur}->{nxt}", u)
        return True

    def shared_notes(self) -> str:
        """The team's taste: human answers + rating comments, capped (written to memory/_shared.md by sea.evolve and
        given to roles as human_answers). Deterministic, so it only changes when the human says something new: the
        newest TASTE_MAX_COMMENTS rating comments, the newest answers up to TASTE_MAX_BULLETS in total, each bullet cut to
        TASTE_MAX_BULLET_CHARS, then the oldest answers (then comments) dropped until it fits in TASTE_MAX_CHARS."""
        def cut(s: str) -> str:
            s = " ".join(str(s).split())
            return s if len(s) <= TASTE_MAX_BULLET_CHARS else s[:TASTE_MAX_BULLET_CHARS - 1] + "…"

        comments = [cut(f"- rating of design {r['genome_id']}: {r['score']}/5 — {r['comment']}")
                    for r in self.human.ratings() if str(r.get("comment", "")).strip()][-TASTE_MAX_COMMENTS:]
        answers = [cut(f"- {p['question']} -> {p['answer']}") for p in self.human.answered_pairs()]
        answers = answers[len(answers) - min(len(answers), TASTE_MAX_BULLETS - len(comments)):]
        while answers or comments:
            text = "\n".join(answers + comments)
            if len(text) <= TASTE_MAX_CHARS:
                return text
            (answers or comments).pop(0)
        return ""

    # ---------------------------------------------------------------- Task protocol
    def describe(self) -> str:
        return DESC.format(brief=self.brief, ver=self.version(), restatement=self.goal()["restatement"])

    def moves(self) -> str:
        return ("hire specialist roles (researcher, persona advocate, critic, scoper/cutter, estimator), fire roles that "
                "add tokens but not quality, rewrite prompts, change topology (draft -> critique loop, debate via "
                "messages, a human role that surfaces the key open question), add tool code (e.g. a checker that "
                "verifies every decision cites an assumption), ask the human about the decision that matters most.")

    def fitness_spec(self) -> FitnessSpec:
        return FitnessSpec([Term("score:fit", 1.0, label="score(fit)"),
                            Term("tokens", -self.lam_tokens, 1000, "team_tokens/1000"),
                            Term("complexity", -self.lam_complexity, 100, "complexity_points/100")])

    def context(self) -> dict:
        d = self.goal()
        return {"brief": self.brief, "restatement": d["restatement"],
                "assumptions": [{k: a.get(k) for k in ("id", "text", "default", "status") if a.get(k)} for a in d["assumptions"]],
                "human_answers": self.shared_notes() or "(none yet)",
                "open_questions": "\n".join(f"- {q['qid']}: {q['question']} (assumed: {q['default']})"
                                            for q in self.human.open_questions()) or "(none)"}

    def episodes(self, split: str) -> list[dict]:
        return [{"id": "brief", "input": {"text": self.brief}, "context": self.context(), "sample": {}}]

    def team_run(self, gdir, worker: str, out_root: Path) -> dict:
        f = out_root / "team.json"
        if f.exists():
            return json.loads(f.read_text())
        g = self.load(gdir)
        res = run_team(g, self.episodes("fit")[0], worker, self.human)
        res["required"] = sorted({*REQUIRED_ARTIFACTS, *(a["name"] for a in g["artifacts"] if a["required"])})
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "artifacts").mkdir(exist_ok=True)
        for k, v in res["artifacts"].items():
            (out_root / "artifacts" / f"{k}.md").write_text(v)
        f.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        return res

    def global_judgement(self, res: dict, out_root: Path) -> dict:
        f = out_root / "judge_global.json"
        if f.exists():
            return json.loads(f.read_text())
        j = judge.judge_global(self.judge_model, self.goal(), res["artifacts"])
        f.write_text(json.dumps(j, ensure_ascii=False, indent=1))
        return j

    def evaluate(self, gdir, split: str, worker: str, out, workers: int = 6, limit: int | None = None) -> dict:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        goal = self.goal()
        try:
            res = self.team_run(gdir, worker, out.parent)
        except Exception as e:
            res = {"artifacts": {}, "tokens": 0, "usage": {}, "questions": [], "assumptions": [], "blackboard": {},
                   "required": list(REQUIRED_ARTIFACTS), "error": repr(e)[:500]}
        present = sum(bool(res["artifacts"].get(a, "").strip()) for a in res["required"]) / len(res["required"])
        glob = self.global_judgement(res, out.parent) if res["artifacts"] else {"scores": {}, "complexity": 0, "features": [],
                                                                               "worst_gap": "no artifacts", "usage": {}}
        scen = [s for s in goal["scenarios"] if s["split"] == split][:limit]
        with cf.ThreadPoolExecutor(max(1, min(self.judge_workers, len(scen)))) as ex:
            js = list(ex.map(lambda s: judge.judge_scenario(self.judge_model, goal, s, res["artifacts"]) if res["artifacts"]
                             else {"scores": {}, "walkthrough": "", "worst_gap": "no artifacts", "usage": {}}, scen))
        crit = {c["id"]: c for c in goal["rubric"]}
        rows = []
        for s, j in zip(scen, js):
            sc = {**{k: v for k, v in glob["scores"].items()}, **j["scores"]}
            score = present * sum(c["weight"] * (sc.get(cid, 1) - 1) / 4 for cid, c in crit.items())
            worst = min(crit, key=lambda cid: sc.get(cid, 1))
            ok = score >= 0.75
            rows.append({"id": s["id"], "persona": s.get("persona"), "score": round(score, 4), "hard": ok,
                         "reason": "ok" if ok else (f"low:{worst}" if present == 1 else "missing_artifact"),
                         "detail": j["worst_gap"], "soft": None, "tokens": res["tokens"], "criteria": sc,
                         "walkthrough": j["walkthrough"], "scenario": s, "judge_usage": j["usage"],
                         "answer": "", "blackboard": {k: str(v)[:1500] for k, v in res.get("blackboard", {}).items()}})
        with open(out / "rows.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        rep = summarize(rows)
        judge_tok = sum(r["judge_usage"].get("prompt", 0) + r["judge_usage"].get("completion", 0) for r in rows)
        judge_tok += glob.get("usage", {}).get("prompt", 0) + glob.get("usage", {}).get("completion", 0)
        rep.update({"genome": self.load(gdir).get("id") if (Path(gdir) / "genome.yaml").exists() else None,
                    "split": split, "worker": worker, "n": len(rows), "secs": round(time.time() - t0, 1),
                    "eval_version": self.version(), "artifacts_present": present,
                    "criteria_mean": {cid: round(sum(r["criteria"].get(cid, 1) for r in rows) / max(len(rows), 1), 2) for cid in crit},
                    "global_worst_gap": glob.get("worst_gap"), "features": glob.get("features", []),
                    "metrics": {"complexity": glob.get("complexity", 0), "judge_tokens": judge_tok},
                    "team_questions": res.get("questions", []), "team_error": res.get("error")})
        (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
        return rep

    def workspace_files(self, parent_eval: Path) -> dict[str, str]:
        parent_eval = Path(parent_eval)
        rep = json.loads((parent_eval / "fit" / "report.json").read_text())
        rows = [json.loads(l) for l in open(parent_eval / "fit" / "rows.jsonl")]
        goal = self.goal()
        fit_personas = {r["persona"] for r in rows}
        files = {
            "reports.json": json.dumps({"fit": rep}, indent=1, ensure_ascii=False),
            "critiques.jsonl": "\n".join(json.dumps({k: r[k] for k in ("id", "persona", "score", "criteria", "walkthrough", "detail",
                                                                        "scenario")}, ensure_ascii=False) for r in rows),
            "rubric.json": json.dumps(goal["rubric"], indent=1, ensure_ascii=False),
            "restatement.md": goal["restatement"],
            "cost_model.md": goal["cost_model"],
            "assumptions.jsonl": "\n".join(json.dumps(a, ensure_ascii=False) for a in goal["assumptions"]),
            "personas.jsonl": "\n".join(json.dumps(p, ensure_ascii=False) for p in goal["personas"] if p["id"] in fit_personas),
            "human.md": self._human_md(),
            "eval_changelog.md": (self.goal_dir / self.version() / "CHANGELOG.md").read_text(),
        }
        for f in (parent_eval / "artifacts").glob("*.md") if (parent_eval / "artifacts").is_dir() else []:
            files[f"parent_artifacts/{f.name}"] = f.read_text()
        return files

    def _human_md(self) -> str:
        ans = self.human.answered_pairs()
        rejected = [a for a in self.goal()["assumptions"] if a.get("status") == "rejected"]
        out = ["# Human input so far", "## Answered"] + [f"- {p['qid']}: {p['question']}\n  assumed: {p['default']}\n  ANSWER: {p['answer']}"
                                                          for p in ans]
        out += ["## Still open (proceed on the default)"] + [f"- {q['qid']}: {q['question']} (default: {q['default']})"
                                                             for q in self.human.open_questions()]
        out += ["## Ratings"] + [f"- {r['genome_id']}: {r['score']}/5 {r['comment']}" for r in self.human.ratings()]
        if rejected:
            out += ["## REVIEW: assumptions the human rejected — every decision that depends on these must be revisited"]
            out += [f"- {a['id']}: {a['text']} (was: {a['default']})" for a in rejected]
        return "\n".join(out)

    def summarize_child(self, ev: dict) -> str:
        r = ev["reports"]["fit"]
        low = sorted(r["criteria_mean"].items(), key=lambda x: x[1])[:2]
        return (f"score(fit) {r['score']}, complexity {r['metrics']['complexity']} pts, team tokens {r['tokens_per_sample']}, "
                f"weakest criteria {low}; global gap: {r.get('global_worst_gap')}")

    def design_log_entry(self, ev_dir: Path) -> str:
        f = Path(ev_dir) / "artifacts" / "decisions.md"
        return f.read_text() if f.exists() else "(no decisions artifact)"

    def cases(self, rows: list[dict], role: str, k_fail: int = 6, k_ok: int = 2) -> str:
        rows = sorted(rows, key=lambda r: r["score"])
        out = []
        for r in rows[:k_fail] + rows[-k_ok:]:
            out.append(f"### scenario {r['id']} (persona {r['persona']}) — score {r['score']}\n"
                       f"judge walk-through: {r['walkthrough']}\nworst gap: {r['detail']}\ncriteria: {r['criteria']}")
        own = rows[0]["blackboard"].get(role, "") if rows else ""
        return f"YOUR OUTPUT in this run (truncated):\n{own[:2500]}\n\nJUDGE FEEDBACK ON THE TEAM'S DESIGN:\n" + "\n\n".join(out)

    def experience(self, gdir, worker: str, out: Path, offset: int = 0, batch: int = 0) -> list[dict]:
        self.evaluate(gdir, "fit", worker, Path(out) / "fit")
        return [json.loads(l) for l in open(Path(out) / "fit" / "rows.jsonl")]
