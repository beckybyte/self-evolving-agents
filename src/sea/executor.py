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
        if sel == "inbox":
            return "\n\n".join(self.inbox.get(role, [])) or "(no messages)"
        if sel == "memory":
            return self.g["_memory"].get(role, "")
        v = self.ep.get("context", {}).get(sel)
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)

    def render(self, template: str, role: str, reads: list[str]) -> str:
        used = set()

        def sub(m):
            sel = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
            known = (m.group(1) in ("conversation", "input", "blackboard", "artifacts", "inbox", "memory", "artifact")
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
            system = "\n\n".join(x for x in [g["soul"], mem_block,
                                            f"## What the team has learned from the human so far\n{shared}" if shared else ""] if x)
            msgs = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": self.render(r["prompt"], name, r["reads"])}]
        text, u = chat(model, msgs, max_tokens=r["max_tokens"], temperature=r["temperature"])
        with self._lock:
            add_usage(self.usage, u)
            if not g["_legacy"]:
                text = strip_think(text)
                self.ask_humans(name, text)
            self.store(r, text)
            self.trace.append({"role": name, "kind": "llm", "tokens": u["prompt"] + u["completion"], "chars": len(text),
                               "model": model, "completion": u["completion"],
                               **{k: u[k] for k in ("finish", "retries", "secs", "fallback") if k in u}})

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

    def run_check_role(self, r: dict):
        """Framework check (sea.checks): runs outside the lock on a snapshot of the artifacts."""
        with self._lock:
            arts = dict(self.artifacts)
        t0 = time.time()
        ok, text = run_check(r["check"], arts, **r.get("options", {}))
        with self._lock:
            self.store(r, text)
            self.trace.append({"role": r["name"], "kind": "check", "tokens": 0, "chars": len(text), "ok": ok,
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

    def step(self, name: str):
        r = next(x for x in self.g["roles"] if x["name"] == name)
        if r["kind"] == "llm":
            return self.run_llm(r)  # takes the lock itself, after the model call
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

    def loop(self, lp: dict):
        """Rounds of the body until stop_when appears in the output of stop_on (default: the last body role).
        stop_on is tested right after that role runs, so a checker first in the body ends the loop before the fixer
        reads anything; check_first also tests it before round 1."""
        stop, on = lp.get("stop_when"), lp.get("stop_on", lp["body"][-1])
        done = lambda: bool(stop) and stop in str(self.bb.get(on, ""))
        if lp.get("check_first") and done():
            return
        for _ in range(int(lp.get("max_rounds", 2))):
            for b in lp["body"]:
                self.step(b)
                if b == on and done():
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
