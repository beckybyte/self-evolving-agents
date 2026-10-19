"""Run one team (normalized genome, see sea.genome) on one episode. Frozen during evolution.

episode = {"id": str, "input": {"messages": [...]} | {"text": str}, "context": {key: str|obj}, "sample": <raw row>}
returns {"answer", "blackboard", "artifacts", "usage", "tokens", "questions", "assumptions", "trace"}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .checks import run_check
from .human import HumanChannel
from .llm import add_usage, chat, strip_think

ROOT = Path(__file__).resolve().parents[2]
ART_RE = re.compile(r"<artifact\s+name=[\"']?([A-Za-z0-9_\-]+)[\"']?\s*>(.*?)</artifact>", re.S)
# legacy emission: a model copying an older input format writes "### artifact: <name>" then the file (fenced or
# not) up to the next such header or the end of the text
LEGACY_ART_RE = re.compile(r"^###\s*artifact:\s*([A-Za-z0-9_\-]+)[ \t]*\n(.*?)(?=^###\s*artifact:|\Z)", re.S | re.M)
FENCE_BLOCK_RE = re.compile(r"^```[^\n]*\n(.*?)\n?^```", re.S | re.M)
LEGACY_MIN_RATIO = 0.5  # a legacy block replaces an existing artifact only if it is at least half as long (not a snippet)


def _slug(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", h.split("(")[0].lower()).strip("-")


def md_sections(text: str) -> dict[str, str]:
    """Markdown sections of a text (## and deeper; a section includes its subsections). Keys: the heading slug,
    and for a ### under a ## '<parent>--<child>' (e.g. v2-additions--extra-endpoints)."""
    lines, heads = text.splitlines(), []
    for i, l in enumerate(lines):
        m = re.match(r"^(#{2,6})\s+(.*)", l)
        if m:
            heads.append((i, len(m.group(1)), _slug(m.group(2))))
    out, parent = {}, ""
    for k, (i, lvl, slug) in enumerate(heads):
        end = next((j for j, l2, _ in heads[k + 1:] if l2 <= lvl), len(lines))
        if lvl == 2:
            parent = slug
        key = slug if lvl == 2 or not parent else f"{parent}--{slug}"
        out.setdefault(key, "\n".join(lines[i:end]).strip())
    return out


def render_artifact(name: str, body: str) -> str:
    """The same format roles must emit, so a role that copies its input format writes a valid artifact."""
    return f'<artifact name="{name}">\n{body}\n</artifact>'


def parse_artifacts(text: str, existing: dict | None = None) -> dict[str, str]:
    """<artifact name="x">...</artifact> blocks, plus legacy "### artifact: x" sections for names not given as tags.
    A legacy section's body is its first fenced block, else the whole section; it does not replace an existing
    artifact with something under LEGACY_MIN_RATIO of its length (a reviewer quoting a snippet)."""
    existing = existing or {}
    out = {an: body.strip() for an, body in ART_RE.findall(text)}
    for an, sec in LEGACY_ART_RE.findall(ART_RE.sub("", text)):
        if an in out:
            continue
        m = FENCE_BLOCK_RE.search(sec)
        body = (m.group(1) if m else sec).strip()
        if body and len(body) >= LEGACY_MIN_RATIO * len(existing.get(an, "")):
            out[an] = body
    return out


ASK_RE = re.compile(r"^\s*ASK_HUMAN:\s*(.+?)\s*\|\s*DEFAULT:\s*(.+?)\s*$", re.M)
PH_RE = re.compile(r"\{(conversation|input|blackboard|artifacts|inbox|memory|[a-z_]+)(?:\.([A-Za-z0-9_\-]+))?\}")
MAX_QUESTIONS_PER_RUN = 3
REPEAT_TEMPERATURE = 0.7  # a role re-asked the identical question is sampled, not replayed
SHARED_KEY = "human_answers"  # context key carrying the same team taste as memory/_shared.md


def render_transcript(messages: list[dict]) -> str:
    return "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages)


def run_tool(gdir: str, tool: str, ctx: dict, messages: list[dict], timeout: int = 60) -> dict:
    """Tools run in a subprocess (they may be LLM-written). run(ctx) (v2) or run(blackboard, messages) (v1)."""
    code = (
        "import json,sys,inspect,importlib.util\n"
        f"spec=importlib.util.spec_from_file_location('t',{str(Path(gdir) / tool)!r})\n"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        "d=json.load(sys.stdin)\n"
        "n=len(inspect.signature(m.run).parameters)\n"
        "r=m.run(d['ctx']['blackboard'],d['messages']) if n>=2 else m.run(d['ctx'])\n"
        "print(json.dumps(r))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    p = subprocess.run([sys.executable, "-c", code], input=json.dumps({"ctx": ctx, "messages": messages}),
                       capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        return {"error": p.stderr[-800:]}
    return json.loads(p.stdout.strip().splitlines()[-1])


class _Run:
    def __init__(self, g: dict, episode: dict, worker: str, human: HumanChannel | None):
        self.g, self.ep, self.worker, self.human = g, episode, worker, human
        self.bb: dict = {}
        self.artifacts: dict = {}
        self.inbox: dict = {r["name"]: [] for r in g["roles"]}
        self.usage: dict = {"prompt": 0, "completion": 0, "calls": 0}
        self.questions: list = []
        self.assumptions: list = []
        self.trace: list = []
        self.messages = episode["input"].get("messages", [])
        self._finish: dict = {}  # role -> finish of its latest model call
        self._seen_calls: set = set()
        self._unchanged: list = []  # (role, hash of messages) already sent in this run
        self._lock = threading.RLock()  # parallel steps: state updates after each role's call are serialized

    # ------------------------------------------------------------ context selectors
    def input_text(self) -> str:
        inp = self.ep["input"]
        return render_transcript(inp["messages"]) if "messages" in inp else str(inp.get("text", ""))

    def select(self, sel: str, role: str) -> str:
        head, _, key = sel.partition(".")
        if sel in ("input", "conversation"):
            return self.input_text()
        if sel == "blackboard":
            return "\n\n".join(f"[{k}]\n{v}" for k, v in self.bb.items() if not k.endswith("_data")) or "(empty)"
        if head == "blackboard":
            return str(self.bb.get(key, ""))
        if sel == "artifacts":
            return "\n\n".join(render_artifact(k, v) for k, v in self.artifacts.items()) or "(none yet)"
        if head == "artifact":
            return render_artifact(key, self.artifacts[key]) if key in self.artifacts else "(not written yet)"
        if head == "artifacts_named_by":
            return self.artifacts_named_by(key)
        if head == "spec":
            return str((self.g.get("specs") or {}).get(key, ""))
        if sel == "inbox":
            return "\n\n".join(self.inbox.get(role, [])) or "(no messages)"
        if sel == "memory":
            return self.g["_memory"].get(role, "")
        v = self.ep.get("context", {}).get(sel)
        if v is None and key and isinstance(self.ep.get("context", {}).get(head), str):
            # <context key>.<section slug>: one markdown section of a context text, e.g. contract.data-shapes
            secs = md_sections(self.ep["context"][head])
            return secs.get(key) or f"(no section {key!r}; sections: {', '.join(secs)})"
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)

    def artifacts_named_by(self, role: str) -> str:
        """Only the file artifacts whose path appears in blackboard[role] (e.g. a smoke report's traceback), so a
        fixer reads the broken file, not every file. The report's own 'files: [...]' listing does not count.
        If the report names no file, all artifacts are given."""
        from .tasks.build import artifact_path  # lazy: tasks.build imports this module
        text = "\n".join(l for l in str(self.bb.get(role, "")).splitlines() if not l.lstrip().startswith("files:"))
        named = [k for k in self.artifacts if (p := artifact_path(k))
                 and re.search(r"(?<![\w.])" + re.escape(p) + r"(?![\w])", text)]
        if not named:
            return "(the report names no file; all artifacts follow)\n\n" + self.select("artifacts", role)
        return "\n\n".join(render_artifact(k, self.artifacts[k]) for k in named)

    def render(self, template: str, role: str, reads: list[str]) -> str:
        used = set()

        def sub(m):
            sel = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
            known = (m.group(1) in ("conversation", "input", "blackboard", "artifacts", "inbox", "memory", "artifact",
                                    "artifacts_named_by", "spec")
                     or m.group(1) in self.ep.get("context", {}))
            if not known:
                return m.group(0)
            used.add("input" if sel == "conversation" else sel)
            return self.select(sel, role)

        text = PH_RE.sub(sub, template)
        extra = [f"## {s}\n{self.select(s, role)}" for s in reads if s not in used and s != "memory"]
        return "\n\n".join(x for x in [text] + extra if x)

    # ------------------------------------------------------------ outputs
    def store(self, role: dict, text: str):
        name = role["name"]
        self.bb[name] = text
        found = parse_artifacts(text, self.artifacts)
        # files re-emitted byte-identical cost output tokens for nothing (evo_v33 gen02 child_5: a fixer re-wrote all
        # 5 files, 32000 tokens, cut off in the 5th); recorded per call for the trace / evolver feedback
        self._unchanged = [n for n, body in found.items() if n in self.artifacts and self.artifacts[n].strip() == body.strip()]
        self.artifacts.update(found)
        w = role.get("writes", "blackboard")
        if w.startswith("artifact:"):
            if w[9:] not in found:
                self.artifacts[w[9:]] = ART_RE.sub("", text).strip() or text
        elif w.startswith("message:"):
            self.inbox[w[8:]].append(f"[from {name}]\n{text}")

    def ask_humans(self, role: str, text: str):
        for q, d in ASK_RE.findall(text):
            if len(self.questions) >= MAX_QUESTIONS_PER_RUN:
                break
            res = self.human.ask(q, d, asker=role, context=self.input_text()[:400]) if self.human else \
                {"qid": None, "answer": d, "answered": False}
            self.questions.append({"role": role, "question": q, "default": d, **res})
            if not res["answered"]:
                self.assumptions.append({"qid": res["qid"], "text": q, "assumed": d, "by": role})

    # ------------------------------------------------------------ roles
    def reads_shared(self, r: dict) -> bool:
        return SHARED_KEY in self.ep.get("context", {}) and (
            SHARED_KEY in r["reads"] or re.search(r"\{" + SHARED_KEY + r"\}", r["prompt"]) is not None)

    def run_llm(self, r: dict):
        g, name = self.g, r["name"]
        model = self.worker if r["model"] == "$WORKER" else r["model"]
        mem = g["_memory"].get(name, "")
        mem_block = f"## Your own notes from past experience in this role (apply them)\n{mem}" if mem else ""
        if g["_legacy"]:  # exact calevo.runner semantics
            if r["native"]:
                sys_extra = "\n\n".join(x for x in [g.get("soul", ""), r.get("prompt", ""), mem_block] if x)
                msgs = [dict(m) for m in self.messages]
                if msgs and msgs[0]["role"] == "system":
                    msgs[0]["content"] = msgs[0]["content"] + ("\n\n" + sys_extra if sys_extra else "")
                elif sys_extra:
                    msgs.insert(0, {"role": "system", "content": sys_extra})
            else:
                prompt = ((mem_block + "\n\n") if mem_block else "") + r["prompt"].replace(
                    "{conversation}", render_transcript(self.messages)).replace(
                    "{blackboard}", json.dumps({k: v for k, v in self.bb.items() if not k.endswith("_data")}, ensure_ascii=False))
                msgs = ([{"role": "system", "content": g["soul"]}] if g["soul"] else []) + [{"role": "user", "content": prompt}]
        else:
            # one source of truth for human knowledge: a role that places the live context key itself does not also
            # get the memory/_shared.md snapshot of the same answers in its system message
            shared = "" if self.reads_shared(r) else g["_memory"].get("_shared", "")
            system = "\n\n".join(x for x in [g["soul"] if r.get("soul", True) else "", mem_block,
                                            f"## What the team has learned from the human so far\n{shared}" if shared else ""] if x)
            msgs = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": self.render(r["prompt"], name, r["reads"])}]
        # a role called again on exactly the same messages (a fix loop whose check report did not change) would, at a
        # low temperature, return the same answer again (evo_mixed_c gen02 child_2: the fixer re-emitted the same broken
        # app.py twice). Such a repeat is sampled at temperature >= REPEAT_TEMPERATURE instead.
        key = (name, hash(json.dumps(msgs, sort_keys=True)))
        with self._lock:
            repeat = key in self._seen_calls
            self._seen_calls.add(key)
        temperature = max(r["temperature"], REPEAT_TEMPERATURE) if repeat else r["temperature"]
        if r.get("images"):
            msgs = self.with_images(msgs, r, model)
        try:
            text, u = chat(model, msgs, max_tokens=r["max_tokens"], temperature=temperature)
        except Exception as e:  # a model call that times out or errors fails this role, not the whole team run
            # (evo_v2_a gen00 r1: a 900 s claude:haiku timeout threw away the run and the ui role's finished file)
            with self._lock:
                self.bb[name] = f"(role {name} failed, no output: {e!r:.300})"
                self.trace.append({"role": name, "kind": "llm", "tokens": 0, "chars": 0, "model": model,
                                   "error": repr(e)[:300]})
            return
        with self._lock:
            add_usage(self.usage, u)
            if not g["_legacy"]:
                text = strip_think(text)
                self.ask_humans(name, text)
            self.store(r, text)
            self._finish[name] = u.get("finish", "")
            self.trace.append({"role": name, "kind": "llm", "tokens": u["prompt"] + u["completion"], "chars": len(text),
                               "model": model, "completion": u["completion"],
                               **({"unchanged_reemits": self._unchanged} if self._unchanged else {}),
                               # what a non-file role (reviewer, planner) said, for the evolver's team traces
                               **({"output_head": ART_RE.sub("", text).strip()[:1500]} if not ART_RE.search(text) else {}),
                               **{k: u[k] for k in ("finish", "retries", "secs", "fallback") if k in u},
                               **({"repeat_temperature": temperature} if repeat else {})})

    def run_tool_role(self, r: dict):
        ctx = {"blackboard": {k: v for k, v in self.bb.items()}, "artifacts": dict(self.artifacts),
               "input": self.ep["input"], "context": self.ep.get("context", {})}
        out = run_tool(self.g["_dir"], r["tool"], ctx, self.messages)
        if isinstance(out, dict):
            text = out.get("text", json.dumps(out))
            self.bb[r["name"] + "_data"] = out
            for an, body in (out.get("artifacts") or {}).items():
                self.artifacts[an] = str(body)
        else:
            text = str(out)
        self.store(r, text)
        self.trace.append({"role": r["name"], "kind": "tool", "tokens": 0, "chars": len(text)})

    def with_images(self, msgs: list, r: dict, model: str) -> list:
        """Attach the screenshots named by `images:` roles (lines 'screenshot: <path>' in their output) to the last
        user message, OpenAI style; Crusoe models only (a Claude CLI worker gets a text note instead)."""
        import base64
        paths = [l.split(":", 1)[1].strip() for src in r["images"] for l in str(self.bb.get(src, "")).splitlines()
                 if l.startswith("screenshot: ") and Path(l.split(":", 1)[1].strip()).is_file()]
        last = dict(msgs[-1])
        if not paths:  # say so: a vision model asked about "the screenshot" otherwise describes one it never saw
            last["content"] += ("\n\n(NO screenshot is attached: the check took none, e.g. the app did not start. "
                                "Do not describe the page.)")
            return msgs[:-1] + [last]
        if not model.startswith("crusoe:"):
            last["content"] += f"\n\n({len(paths)} screenshot(s) of the page exist, but this model cannot see images.)"
            return msgs[:-1] + [last]
        last["content"] = [{"type": "text", "text": last["content"]}] + [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(Path(p).read_bytes()).decode()}}
            for p in paths[:3]]
        return msgs[:-1] + [last]

    def run_check_role(self, r: dict):
        """Framework check (sea.checks): runs outside the lock on a snapshot of the artifacts."""
        with self._lock:
            arts = dict(self.artifacts)
        t0 = time.time()
        ok, text = run_check(r["check"], arts, **r.get("options", {}))
        with self._lock:
            self.store(r, text)
            self.trace.append({"role": r["name"], "kind": "check", "tokens": 0, "chars": len(text), "ok": ok,
                               "report": text[:2000],  # the evolver reads team traces: show why a check failed
                               "secs": round(time.time() - t0, 1)})

    def run_human(self, r: dict):
        q = self.render(r["question"], r["name"], [])
        d = self.render(r["default"], r["name"], [])
        if self.human and q.strip():
            res = self.human.ask(q, d, asker=r["name"])
        else:
            res = {"qid": None, "answer": d, "answered": False}
        if not res["answered"]:
            self.assumptions.append({"qid": res["qid"], "text": q, "assumed": d, "by": r["name"]})
        self.questions.append({"role": r["name"], "question": q, "default": d, **res})
        text = res["answer"] if res["answered"] else f"(no human answer yet; proceed on this assumption) {d}"
        self.store(r, text)
        self.trace.append({"role": r["name"], "kind": "human", "tokens": 0, "answered": res["answered"]})

    def skip(self, r: dict) -> bool:
        """when: {on, contains | not_contains}: False = run the role, True = skip it (0 tokens, trace kind skip)."""
        wh = r.get("when")
        if not wh:
            return False
        out = str(self.bb.get(wh["on"], ""))
        run = (wh["contains"] in out) if "contains" in wh else (wh["not_contains"] not in out)
        if not run:
            with self._lock:
                self.trace.append({"role": r["name"], "kind": "skip", "tokens": 0, "when": wh})
        return not run

    def step(self, name: str):
        r = next(x for x in self.g["roles"] if x["name"] == name)
        if self.skip(r):
            return
        if r["kind"] == "llm":
            self.run_llm(r)  # takes the lock itself, after the model call
            missing = [a for a in r.get("expects") or [] if a not in parse_artifacts(str(self.bb.get(name, "")))]
            if missing:  # the role did not emit a file it must write: run it once more (counted)
                with self._lock:
                    self.trace.append({"role": name, "kind": "retry", "tokens": 0, "missing": missing})
                self.run_llm(r)
            return
        if r["kind"] == "check":
            return self.run_check_role(r)  # may take tens of seconds: not under the lock
        with self._lock:
            {"tool": self.run_tool_role, "human": self.run_human}[r["kind"]](r)

    def parallel(self, names: list[str]):
        """Run roles concurrently. Each renders its prompt from the state before the step (prompts are built before
        any model call returns); their writes land as each finishes. A failing role fails the whole step."""
        with ThreadPoolExecutor(len(names)) as ex:
            for f in [ex.submit(self.step, n) for n in names]:
                f.result()

    def says(self, role: str, marker: str) -> bool:
        """A stop marker counts only at the start of a line (after markdown bullets/emphasis), and never in an answer
        cut off at max_tokens. evo_v3_b gen00 r1: a truncated reviewer quoted its instruction '... do NOT write
        ALL_CLEAR' mid-sentence, the substring matched, the fix loop stopped with smoke failing (run 0.09)."""
        if self._finish.get(role) == "length":
            return False
        return any(l.strip().lstrip("*#->`_ ").startswith(marker) for l in str(self.bb.get(role, "")).splitlines())

    def loop(self, lp: dict):
        """Rounds of the body until stop_when appears in the output of stop_on (default: the last body role).
        stop_on is tested right after that role runs, so a checker first in the body ends the loop before the fixer
        reads anything; check_first also tests it before round 1."""
        last = lp["body"][-1]
        stop, on = lp.get("stop_when"), lp.get("stop_on", last["parallel"][-1] if isinstance(last, dict) else last)
        conds = [(c["on"], c["when"]) for c in lp.get("stop_all") or []] or ([(on, stop)] if stop else [])
        done = lambda: bool(conds) and all(self.says(o, w) for o, w in conds)
        if lp.get("check_first") and done():
            return
        for _ in range(int(lp.get("max_rounds", 2))):
            for b in lp["body"]:
                if isinstance(b, dict):
                    self.parallel(list(b["parallel"]))
                    ran = list(b["parallel"])
                else:
                    self.step(b)
                    ran = [b]
                if (on in ran or bool(lp.get("stop_all"))) and done():
                    return

    def run(self) -> dict:
        for s in self.g["workflow"]:
            if isinstance(s, dict) and "parallel" in s:
                self.parallel(list(s["parallel"]))
            elif isinstance(s, dict):
                self.loop(s["loop"])
            else:
                self.step(s)
        ans_ref = self.g["outputs"].get("answer")
        answer = ""
        if ans_ref:
            head, _, key = ans_ref.partition(".")
            answer = self.bb.get(key, "") if head == "blackboard" else self.artifacts.get(key, "")
        return {"answer": answer, "blackboard": self.bb, "artifacts": self.artifacts, "usage": self.usage,
                "tokens": self.usage["prompt"] + self.usage["completion"], "questions": self.questions,
                "assumptions": self.assumptions, "trace": self.trace}


def run_team(g: dict, episode: dict, worker: str, human: HumanChannel | None = None) -> dict:
    return _Run(g, episode, worker, human).run()
