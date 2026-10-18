"""Genome v2: a team = roles (llm | tool | human | check) + workflow + communication channel + declared artifacts.

genome.yaml (schema: 2)
  id, parent, notes                 notes = what changed vs the parent and which evidence motivated it
  mechanism: str                    optional short tag for the diversity archive (e.g. "draft-critique-loop")
  soul: str                         shared system prompt for every llm role
  specs: {name: text}               optional named spec sections, read per role with the selector spec.<name>
  channels: blackboard | messages   blackboard: every role reads/writes one shared dict (default)
                                    messages: additionally each role has an inbox; writes: message:<role> delivers
  roles:
    - name: str                     unique
      kind: llm | tool | human | check   (v1 spelling `type:` accepted)
      # llm
      model: $WORKER | qwen3-8b | qwen3-30b-a3b | qwen2.5-72b | claude:<alias>
      reads: [selector, ...]        what the role sees; default [input, blackboard]. selectors:
                                      input, blackboard, blackboard.<key>, artifacts, artifact.<name>, inbox, memory,
                                      spec.<name> (one entry of the genome's `specs:` dict, so a spec section
                                      reaches only the roles that read it instead of every role via soul),
                                      artifacts_named_by.<role> (only the files whose path appears in that role's
                                      output, e.g. a smoke report: a fixer then reads the broken file only),
                                      or any context key the task provides (brief mode: restatement, assumptions,
                                      human_answers). A selector used as {placeholder} in the prompt is substituted
                                      there; the others are appended as "## <selector>" sections.
      prompt: str                   templating: ONLY {selector} with a known selector name (input, blackboard,
                                    blackboard.<key>, artifacts, artifact.<name>, inbox, memory, conversation, or a
                                    task context key) is replaced; every other brace, e.g. literal JSON
                                    {"ok": true}, is left as written (no str.format); soul is never templated
      writes: blackboard | artifact:<name> | message:<role>       (default blackboard; always also blackboard[name])
      max_tokens: int = 2048, temperature: float = 0.0
      expects: [artifact names]     the role must emit these artifacts in its own answer; if one is missing (empty or
                                    cut-short answer) the role is run once more (both calls counted)
      # tool
      tool: tools/<file>.py         run(ctx) -> {"text": str, "artifacts": {name: str}?}
                                    ctx = {"blackboard", "artifacts", "input", "context"}; v1 run(blackboard, messages) also works
      # check (framework-provided, frozen code in sea.checks; the genome only names it)
      check: app_smoke              build mode: materialise the current file artifacts, compile every .py, start
                                    app.py and make a few generic contract calls; output (blackboard[name]) is a
                                    report starting "SMOKE: PASS" or "SMOKE: FAIL (...)" with compile errors, the
                                    traceback tail, HTTP status and JSON-shape problems per call. 0 tokens, <= 40 s.
      calls: basic | deep | v2      app_smoke option (default basic: reset, state, command "call mom" dump, focus).
                                    v2 = deep plus the contract-v2 endpoints (log, import of a one-event .ics -> plan
                                    apply/undo, learn, beliefs (+ delete one the app returned), proposal, view week,
                                    prompts, activity), crash/shape only.
                                    deep also chains chat "yes" on the created item -> items/<id>/done -> clock +2 h
                                    -> undo (when a message has an undo_id); still crash/shape problems only, and a
                                    slow deep call (timeout, budget used up) is a note, not a failure.
      selftest_secs: 12 | 20        time limit of that self-test (default 12)
      selftest: true                app_smoke option: first runs `python app.py --selftest --data <tmp>` (<= 12 s, must
                                    exit) and reports its exit code and output; nonzero exit = a problem. The team
                                    writes the self-test; the normal start-up path is still what gets started and scored.
      # human (never blocks)
      question: str (template), default: str (template)  -> output = the human's answer if one exists, else the default
  workflow: [step, ...]             step = role name, or {loop: {body: [names], max_rounds: int, stop_when: str}}
                                    (a stop marker counts only at the START of a line of that output (markdown
                                    bullets/bold allowed), never mid-sentence and never in an answer cut off at
                                    max_tokens)
                                    (loop stops early when the output of stop_on (a body role; default the last
                                    one) contains stop_when, tested right after stop_on runs: body [checker, fixer]
                                    with stop_on: checker skips the fixer once the checker says ALL_CLEAR;
                                    check_first: true also tests it BEFORE round 1, so the loop runs 0 rounds when
                                    e.g. a smoke check that ran just before the loop already passed;
                                    stop_all: [{on: smoke, when: "SMOKE: PASS"}, {on: auditor, when: ALL_CLEAR}]
                                    instead stops only when ALL hold, tested after every body role)
                                    or {parallel: [names]}: 2..6 roles run concurrently, each starting from the state
                                    before the step (use it for independent pieces, e.g. backend / ui). Default: roles in order.
  artifacts: [{name, format: markdown|json|text, required: bool=true}]
  outputs: {answer: blackboard.<key> | artifact.<name>}     what a benchmark scores (v1 `final: X` -> blackboard.X)

Any llm role may also
  - emit `<artifact name="prd">...</artifact>` blocks (stored as artifacts[prd]), and
  - emit lines `ASK_HUMAN: <question> | DEFAULT: <best guess>`: posted to the inbox, the run continues on the default,
    which is recorded as an assumption (max 3 per run).
Per-role notes live in memory/<role>.md (injected into that role's prompt); memory/_shared.md is read by every llm role
(in its system message, unless the role already reads the task's human_answers context key, which carries the same text).

v1 genomes (no `schema:` key; `final`, `input: native|transcript`, `{conversation}`) are adapted on load and run
exactly as calevo.runner ran them.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .llm import known_model

KINDS = ("llm", "tool", "human", "check")
BASE_SELECTORS = ("input", "blackboard", "artifacts", "inbox", "memory")


def load_genome(gdir: str | Path, context_keys: tuple[str, ...] = ()) -> dict:
    gdir = Path(gdir)
    raw = yaml.safe_load(open(gdir / "genome.yaml")) or {}
    g = normalize(raw)
    g["_dir"] = str(gdir)
    mdir = gdir / "memory"
    g["_memory"] = {f.stem: f.read_text().strip() for f in mdir.glob("*.md")} if mdir.is_dir() else {}
    validate(g, context_keys)
    return g


def normalize(raw: dict) -> dict:
    legacy = "schema" not in raw
    roles = []
    for r in raw.get("roles") or []:
        kind = r.get("kind", r.get("type", "llm"))
        n = {"name": r.get("name"), "kind": kind}
        if kind == "llm":
            native = legacy and r.get("input", "native") == "native"
            n.update(model=r.get("model", "$WORKER"), prompt=r.get("prompt", "") or "",
                     max_tokens=int(r.get("max_tokens", 2048)), temperature=float(r.get("temperature", 0.0)),
                     native=native, writes=r.get("writes", "blackboard"),
                     reads=list(r.get("reads") or (["input", "blackboard"])), expects=list(r.get("expects") or []))
            if legacy:
                n["_v1_input"] = r.get("input", "native")
        elif kind == "tool":
            n.update(tool=r.get("tool"), writes=r.get("writes", "blackboard"))
        elif kind == "check":
            n.update(check=r.get("check"), writes=r.get("writes", "blackboard"),
                     options={k: v for k, v in r.items() if k not in ("name", "kind", "type", "check", "writes")})
        elif kind == "human":
            n.update(question=r.get("question", ""), default=r.get("default", ""), writes=r.get("writes", "blackboard"),
                     reads=list(r.get("reads") or ["blackboard"]))
        roles.append(n)
    if legacy:
        outputs = {"answer": f"blackboard.{raw.get('final')}"}
    else:
        outputs = raw.get("outputs") or {}
    return {"schema": 2, "id": raw.get("id"), "parent": raw.get("parent"), "notes": raw.get("notes", ""),
            "mechanism": raw.get("mechanism", ""), "soul": raw.get("soul", "") or "",
            "channels": raw.get("channels", "blackboard"), "roles": roles,
            "workflow": raw.get("workflow") or [r["name"] for r in roles],
            "artifacts": [{"name": a["name"], "format": a.get("format", "markdown"), "required": a.get("required", True)}
                          for a in (raw.get("artifacts") or [])],
            "outputs": outputs, "specs": {str(k): str(v) for k, v in (raw.get("specs") or {}).items()},
            "_legacy": legacy}


def _selector_ok(s: str, names: set, context_keys) -> bool:
    if s in BASE_SELECTORS or s in context_keys:
        return True
    head, _, tail = s.partition(".")
    return head in ("blackboard", "artifact", "artifacts_named_by", "spec") and bool(tail)


def validate(g: dict, context_keys: tuple[str, ...] = ()):
    assert g["roles"], "roles must be a non-empty list"
    names: set = set()
    for r in g["roles"]:
        assert r.get("name") and r["name"] not in names, f"bad/duplicate role name {r.get('name')}"
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]*", r["name"]), f"role name must be an identifier: {r['name']}"
        names.add(r["name"])
        assert r["kind"] in KINDS, f"unknown kind {r['kind']} (llm | tool | human | check)"
        if r["kind"] == "check":
            from .checks import CHECKS
            assert r.get("check") in CHECKS, f"role {r['name']}: unknown check {r.get('check')!r} ({sorted(CHECKS)})"
            from .checks import CHECK_OPTIONS
            allowed = CHECK_OPTIONS.get(r["check"], {})
            for k, v in r.get("options", {}).items():
                assert k in allowed and v in allowed[k], (
                    f"role {r['name']}: check option {k}: {v!r} not allowed ({ {o: list(a) for o, a in allowed.items()} })")
        if r["kind"] == "llm":
            assert known_model(r["model"]), f"unknown model {r['model']}"
            if g["_legacy"]:
                assert r["_v1_input"] in ("native", "transcript"), "v1 input must be native|transcript"
            for s in r["reads"]:
                assert _selector_ok(s, names, context_keys), f"role {r['name']}: unknown reads selector {s!r}"
                assert not s.startswith("spec.") or s[5:] in g.get("specs", {}), f"role {r['name']}: no spec {s[5:]!r} in specs"
            assert all(re.fullmatch(r"[A-Za-z0-9_\-]+", a) for a in r.get("expects", [])), \
                f"role {r['name']}: expects must list artifact names"
        if r["kind"] == "tool":
            assert r.get("tool") and (Path(g["_dir"]) / r["tool"]).exists(), f"missing tool file {r.get('tool')}"
        w = r.get("writes", "blackboard")
        assert w == "blackboard" or re.fullmatch(r"(artifact|message):[A-Za-z0-9_\-]+", w), f"bad writes {w!r}"
    for r in g["roles"]:
        w = r.get("writes", "")
        if w.startswith("message:"):
            assert g["channels"] == "messages", "writes: message:<role> needs channels: messages"
            assert w[8:] in names, f"message target {w[8:]} is not a role"
    for step in g["workflow"]:
        if isinstance(step, dict) and "parallel" in step:
            par = step["parallel"]
            assert isinstance(par, list) and 2 <= len(par) <= 6 and len(set(par)) == len(par), \
                f"parallel needs 2..6 distinct roles: {step}"
            assert all(p in names for p in par), f"bad parallel step {step}"
        elif isinstance(step, dict):
            lp = step.get("loop") or {}
            assert lp.get("body") and all(b in names for b in lp["body"]), f"bad loop {step}"
            assert 1 <= int(lp.get("max_rounds", 2)) <= 6, "loop max_rounds must be 1..6"
            assert lp.get("stop_on", lp["body"][-1]) in lp["body"], f"loop stop_on must be a body role: {step}"
            for c in lp.get("stop_all") or []:
                assert isinstance(c, dict) and c.get("on") in lp["body"] and c.get("when"), \
                    f"loop stop_all entries are {{on: <body role>, when: <text>}}: {step}"
            assert not (lp.get("stop_all") and (lp.get("stop_when") or lp.get("stop_on"))), \
                f"loop: use stop_all or stop_when/stop_on, not both: {step}"
        else:
            assert step in names, f"workflow step {step!r} is not a role"
    ans = g["outputs"].get("answer")
    if ans:
        head, _, key = ans.partition(".")
        assert head in ("blackboard", "artifact") and key, f"bad outputs.answer {ans!r}"
        if head == "blackboard":
            assert key in names, "outputs.answer must name a role (v1: final must name a role)"


BANNED_TOOL_PATTERNS = re.compile(r"data/splits|\bgoal/|holdout|\.\./|/hot-data/|/home/|ratings\.jsonl")


def check_tool_safety(gdir: str | Path, extra: re.Pattern | None = None):
    """Evolver-written tool code must not reach evaluation data (static check; tools also run in a subprocess)."""
    for f in Path(gdir).rglob("*.py"):
        src = f.read_text()
        for pat in filter(None, (BANNED_TOOL_PATTERNS, extra)):
            m = pat.search(src)
            assert not m, f"forbidden reference in {f.name}: {m.group(0)!r}"


def structure_signature(g: dict) -> str:
    """Coarse structural fingerprint (used by the radical-child rule and the archive)."""
    kinds = "".join(r["kind"][0] for r in g["roles"])
    loops = sum(isinstance(s, dict) and "loop" in s for s in g["workflow"])
    par = sum(isinstance(s, dict) and "parallel" in s for s in g["workflow"])
    return f"{kinds}|{g['channels'][0]}|L{loops}|S{len(g['workflow'])}" + (f"|P{par}" if par else "")
