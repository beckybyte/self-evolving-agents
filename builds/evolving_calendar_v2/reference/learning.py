"""v2 learning loop: interaction log, habit learner (beliefs), views + hidden_counts, proposal gate,
meta-learner with versioned prompts, activity feed. Mixed into App (app.py)."""
import datetime as dt
import difflib
import hashlib
import re
import uuid

import llm
from timeparse import from_iso, iso

THRESHOLD = 0.7
RESHOW_DELTA = 0.15
REVERT_PENALTY = 0.1
CORE = {
    "designer": "You propose at most one calendar view change at a time. Every proposal cites the belief and the "
                "number it rests on. Nothing changes until the person accepts. Every view reports hidden items. "
                "Kind wording only.",
    "followthrough": "You remind gently and move unfinished tasks forward quietly with an undo. Name the task and the "
                     "new time. Never use guilt words. On the third move, ask instead of moving.",
    "learner": "You learn habits only from what the person did. Each belief states its number, has a confidence "
               "between 0 and 1, and cites evidence. Deleted beliefs stay deleted for the same evidence.",
}
DEFAULT_META = {"novelty": 0.6, "cadence_hours": 6, "breakdown_appetite": 0.5, "tone": "gentle"}
FORBIDDEN = re.compile(r"\b(ignore|override|core rules?|disregard|jailbreak)\b", re.I)
WEEKDAYS5 = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


class LearningMixin:
    # ------------------------------------------------------------ state
    def wipe_learning(self):
        self.log = []
        self.beliefs = {}           # key -> Belief
        self.deleted_beliefs = {}   # key -> evidence ids at deletion
        self.penalty = {}           # key -> confidence penalty from reverted proposals
        self.proposals = []
        self.view = {"type": "week", "params": {}}
        self.meta_state = dict(DEFAULT_META)
        self.activity = []
        self.learn_mark = 0
        self.prompts = []
        for agent, core in CORE.items():
            self.prompts.append({"agent": agent, "version": 1, "core_hash": h(core), "learned": "", "diff": "",
                                 "reason": "initial version", "evidence": [],
                                 "replay": {"passed": True, "detail": "baseline"}, "active": True,
                                 "_novelty": DEFAULT_META["novelty"]})
        self.asked_breaks = 0
        self.yes_breaks = 0

    LEARN_FIELDS = ("log", "beliefs", "deleted_beliefs", "penalty", "proposals", "view", "meta_state", "activity",
                    "learn_mark", "prompts", "asked_breaks", "yes_breaks")

    # ------------------------------------------------------------ log + activity
    def log_add(self, kind, item_ids=(), payload=None, source="person", at=None):
        e = {"id": "lg_" + uuid.uuid4().hex[:8], "at": at or self.now, "kind": kind,
             "item_ids": [i for i in item_ids if i], "payload": payload or {}, "source": source}
        self.log.append(e)
        return e

    def act(self, agent, kind, text):
        self.activity.append({"at": self.now, "agent": agent, "kind": kind, "text": text})

    def maybe_learn(self):
        if len(self.log) - self.learn_mark >= 5:
            self.learn()

    # ------------------------------------------------------------ habit learner
    def _evidence_id(self, item_id):
        it = self.items.get(item_id)
        if it and it.get("source") == "seed":
            return item_id
        for e in self.log:
            if item_id in e["item_ids"] and e["kind"] in ("create", "done", "import", "plan_applied"):
                return e["id"]
        return None

    def _finish_time(self, it):
        m = self.meta.get(it["id"], {})
        if m.get("done_by_person"):
            return from_iso(m.get("done_at"))
        return from_iso(it.get("end")) or from_iso(it.get("start")) or from_iso(m.get("done_at"))

    def compute_beliefs(self):
        found = {}
        # evening work: done tasks finished at or after 20:00
        done = [it for it in self.items.values() if it["status"] == "done" and it["kind"] == "task"]
        times = [(it, self._finish_time(it)) for it in done]
        times = [(it, t) for it, t in times if t]
        if times:
            ev = [it for it, t in times if t.hour >= 20]
            k, n = len(ev), len(times)
            ids = []
            for it, t in times:
                eid = self._done_evidence(it)
                if eid:
                    ids.append(eid)
            found["evening_worker"] = {
                "level": "habit", "statement": f"Finishes tasks after 8 PM ({k} of {n})",
                "value": {"after_hour": 20, "count": k, "total": n}, "raw": (k + 1) / (n + 2), "evidence": ids}
        # weekends: weeks with items and none on a weekend
        dated = [(it, from_iso(it["start"])) for it in self.items.values() if it["start"] and it["kind"] != "note"]
        dated = [(it, d) for it, d in dated if d]
        if dated:
            weeks = {}
            for it, d in dated:
                wk = d.isocalendar()[:2]
                weeks.setdefault(wk, [0, 0])
                weeks[wk][0] += 1
                if d.weekday() >= 5:
                    weeks[wk][1] += 1
            free = sum(1 for a, w in weeks.values() if w == 0)
            n_we = sum(w for a, w in weeks.values())
            nw = len(weeks)
            if n_we == 0:
                st = f"No weekend plans in {nw} weeks (0 of {len(dated)} items on a weekend)"
            else:
                st = f"Weekends free in {free} of {nw} weeks ({n_we} weekend items)"
            ids = [x for x in (self._evidence_id(it["id"]) for it, _ in dated) if x][:60]
            found["no_weekends"] = {"level": "habit", "statement": st,
                                    "value": {"free_weeks": free, "weeks": nw, "weekend_items": n_we},
                                    "raw": (free + 1) / (nw + 2), "evidence": ids}
        # meta: invented layouts undone
        inv = [p for p in self.proposals if p["novelty"] == "invented" and p["status"] in ("accepted", "reverted")]
        if inv:
            r = sum(1 for p in inv if p["status"] == "reverted")
            ids = [e["id"] for e in self.log if e["kind"] == "proposal_reverted"]
            found["prefers_classic_views"] = {
                "level": "meta", "statement": f"Undid {r} of {len(inv)} new-style view ideas, so classic views first",
                "value": {"reverted": r, "tried": len(inv)}, "raw": (r + 1) / (len(inv) + 2), "evidence": ids}
        if self.asked_breaks:
            found["breakdown_appetite"] = {
                "level": "meta", "statement": f"Said yes to tiny steps {self.yes_breaks} of {self.asked_breaks} times",
                "value": self.yes_breaks / self.asked_breaks,
                "raw": (self.yes_breaks + 1) / (self.asked_breaks + 2),
                "evidence": [e["id"] for e in self.log if e["kind"] == "chat"][-10:]}
        return found

    def _done_evidence(self, it):
        if it.get("source") == "seed" and not self.meta.get(it["id"], {}).get("done_by_person"):
            return it["id"]
        for e in reversed(self.log):
            if e["kind"] == "done" and it["id"] in e["item_ids"]:
                return e["id"]
        return self._evidence_id(it["id"])

    def update_beliefs(self):
        found = self.compute_beliefs()
        for key, b in found.items():
            if key in self.deleted_beliefs and set(b["evidence"]) <= set(self.deleted_beliefs[key]):
                continue
            self.deleted_beliefs.pop(key, None)
            conf = round(max(0.0, min(1.0, b["raw"] - self.penalty.get(key, 0.0))), 3)
            old = self.beliefs.get(key)
            new = {"key": key, "level": b["level"], "statement": b["statement"], "value": b["value"],
                   "confidence": conf, "evidence": b["evidence"], "updated_at": self.now}
            if not old or abs(old["confidence"] - conf) >= 0.01 or old["statement"] != b["statement"]:
                self.act("learner", "belief_update", f"{b['statement']}: confidence {conf:.2f}"
                         + (f" (was {old['confidence']:.2f})" if old else " (new)"))
                if b["level"] == "habit" and conf >= THRESHOLD and (not old or old["confidence"] < THRESHOLD):
                    self.act("learner", "threshold_crossed", f"{b['statement']} reached {conf:.2f} (threshold {THRESHOLD})")
                self.beliefs[key] = new
            else:
                old["evidence"] = b["evidence"]
        for key in list(self.beliefs):
            if key not in found:
                del self.beliefs[key]
        if "breakdown_appetite" in self.beliefs:
            self.meta_state["breakdown_appetite"] = round(self.beliefs["breakdown_appetite"]["confidence"], 3)
        return list(self.beliefs.values())

    def delete_belief(self, key):
        b = self.beliefs.pop(key, None)
        if not b:
            return False
        self.deleted_beliefs[key] = list(b["evidence"])
        e = self.log_add("belief_deleted", [], {"key": key, "statement": b["statement"]}, "person")
        self.act("learner", "belief_deleted", f"Forgot on request: {b['statement']}")
        _ = e
        return True

    def learn(self):
        self.learn_mark = len(self.log)
        beliefs = self.update_beliefs()
        prop = self.gate()
        self.learn_mark = len(self.log)
        return {"beliefs": beliefs, "proposal": prop or self.open_proposal()}

    # ------------------------------------------------------------ views
    def view_spec(self, view=None):
        v = dict(view or self.view)
        v["params"] = dict(v.get("params") or {})
        v["hidden_counts"] = self.hidden_counts(v)
        return v

    def _hidden(self, v, d, it):
        t, p = v["type"], v.get("params") or {}
        if t == "workweek" or p.get("hide_weekends"):
            if d.weekday() >= 5:
                return True
        if "start_hour" in p or "end_hour" in p:
            sh, eh = p.get("start_hour", 0), p.get("end_hour", 24)
            e = self.eff_end(it) or d
            if e <= d.replace(hour=sh, minute=0) or d >= d.replace(hour=0, minute=0) + dt.timedelta(hours=eh):
                return True
        return False

    def hidden_counts(self, v):
        out = {}
        for it in self.items.values():
            d = from_iso(it["start"])
            if not d or it["kind"] == "note":
                continue
            if self._hidden(v, d, it):
                k = d.date().isoformat()
                out[k] = out.get(k, 0) + 1
        return out

    def set_view(self, vtype):
        if vtype not in ("month", "week", "workweek", "day"):
            vtype = "week"
        self.view = {"type": vtype, "params": {}}
        self.log_add("view_switch", [], {"type": vtype}, "person")
        return self.view_spec()

    # ------------------------------------------------------------ proposals
    def open_proposal(self):
        for p in self.proposals:
            if p["status"] == "shown":
                return self.pub(p)
        return None

    def pub(self, p):
        return {k: v for k, v in p.items() if not k.startswith("_")}

    def _candidate(self, b, invented):
        n = b["statement"]
        if b["key"] == "no_weekends":
            if invented:
                return ("Try a weekday strip with weekends folded away?",
                        {"type": "custom", "params": {"name": "weekday strip", "days": WEEKDAYS5,
                                                      "hide_weekends": True, "weekend": "folded into one line"}},
                        f"{n}. Fold weekends into a single line?")
            return ("Hide weekends?", {"type": "workweek", "params": {}}, f"{n}. Hide weekends?")
        if b["key"] == "evening_worker":
            if invented:
                return ("Try an evening lens?",
                        {"type": "custom", "params": {"name": "evening lens", "start_hour": 17, "end_hour": 24,
                                                      "days": 3}},
                        f"{n}. Show the next three evenings side by side?")
            return ("Open on today, evening first?", {"type": "day", "params": {"start_hour": 7, "end_hour": 24,
                                                                                "scroll_to": 20}},
                    f"{n}. Open your calendar on today's evening?")
        return None

    def gate(self):
        if self.open_proposal():
            return None
        now = self.nowdt
        shown_times = [from_iso(p["_shown_at"]) for p in self.proposals if p.get("_shown_at")]
        if shown_times and now - max(shown_times) <= dt.timedelta(hours=self.meta_state["cadence_hours"]):
            return None
        cands = []
        invented = self.meta_state["novelty"] >= 0.5
        dv = self.active_prompt("designer")["version"]
        for b in self.beliefs.values():
            if b["level"] != "habit" or b["confidence"] < THRESHOLD:
                continue
            # an undone *invented* layout does not block a classic view once the designer prompt has learned
            # from it (the person turned down the layout style, not the habit)
            blocked = any(p["status"] in ("dismissed", "reverted") and b["key"] in p["belief_keys"]
                          and b["confidence"] <= p["_closed_conf"] + RESHOW_DELTA
                          and not (p["status"] == "reverted" and p["novelty"] == "invented" and not invented
                                   and p["prompt_versions"].get("designer") != dv
                                   and not any(q["status"] in ("dismissed", "reverted") and q["novelty"] == "classic"
                                               and b["key"] in q["belief_keys"] for q in self.proposals))
                          for p in self.proposals)
            if blocked:
                continue
            if any(p["status"] == "accepted" and b["key"] in p["belief_keys"] for p in self.proposals):
                continue
            cands.append(b)
        if not cands:
            return None
        b = max(cands, key=lambda x: x["confidence"])
        title, view, reason = self._candidate(b, invented)
        p = {"id": "pr_" + uuid.uuid4().hex[:8], "title": title, "reason": reason, "view": self.view_spec(view),
             "belief_keys": [b["key"]], "novelty": "invented" if invented else "classic",
             "prompt_versions": {a: self.active_prompt(a)["version"] for a in CORE}, "status": "shown",
             "_shown_at": self.now, "_conf": b["confidence"], "_closed_conf": b["confidence"], "_prev_view": None}
        self.proposals.append(p)
        self.log_add("proposal_shown", [], {"proposal_id": p["id"], "belief": b["key"], "novelty": p["novelty"]},
                     "agent")
        self.act("designer", "proposal", f"Proposed: {title} Because: {reason} ({p['novelty']})")
        return self.pub(p)

    def proposal_action(self, pid, action):
        p = next((x for x in self.proposals if x["id"] == pid), None)
        if not p:
            return None
        if action == "accept" and p["status"] == "shown":
            p["_prev_view"] = dict(self.view)
            self.view = {"type": p["view"]["type"], "params": dict(p["view"]["params"])}
            p["status"] = "accepted"
            self.log_add("proposal_accepted", [], {"proposal_id": pid, "view": self.view}, "person")
            self.act("designer", "proposal_accepted", f"Accepted: {p['title']}")
        elif action == "dismiss" and p["status"] == "shown":
            p["status"] = "dismissed"
            p["_closed_conf"] = self._conf(p)
            self.log_add("proposal_dismissed", [], {"proposal_id": pid}, "person")
            self.act("designer", "proposal_dismissed", f"Not now: {p['title']}")
        elif action == "undo" and p["status"] in ("accepted", "shown"):
            if p["status"] == "accepted" and p.get("_prev_view"):
                self.view = dict(p["_prev_view"])
            p["status"] = "reverted"
            for k in p["belief_keys"]:
                self.penalty[k] = self.penalty.get(k, 0.0) + REVERT_PENALTY
            self.log_add("proposal_reverted", [], {"proposal_id": pid, "novelty": p["novelty"]}, "person")
            self.act("designer", "proposal_reverted", f"Undone: {p['title']}")
            self.update_beliefs()
            p["_closed_conf"] = self._conf(p)
            self.meta_learn()
        return {"proposal": self.pub(p), "view": self.view_spec()}

    def _conf(self, p):
        return max([self.beliefs[k]["confidence"] for k in p["belief_keys"] if k in self.beliefs] or [0.0])

    # ------------------------------------------------------------ prompts + meta-learner
    def active_prompt(self, agent):
        return next(v for v in reversed(self.prompts) if v["agent"] == agent and v["active"])

    def meta_learn(self):
        reverted = [p for p in self.proposals if p["status"] == "reverted" and p["novelty"] == "invented"
                    and not p.get("_meta_used")]
        if len(reverted) < 2:
            return None
        cur = self.active_prompt("designer")
        old_nov = self.meta_state["novelty"]
        new_nov = round(max(0.0, min(old_nov, 0.5) - 0.2), 2)
        line = self._learned_line(len(reverted))
        lines = [x for x in cur["learned"].splitlines() if x.strip()]
        lines.append(line)
        lines = lines[-12:]
        learned = "\n".join(lines)
        diff = "\n".join(difflib.unified_diff(cur["learned"].splitlines(), learned.splitlines(),
                                              f"designer v{cur['version']}", f"designer v{cur['version'] + 1}",
                                              lineterm=""))
        evidence = [e["id"] for e in self.log if e["kind"] == "proposal_reverted"
                    and e["payload"].get("proposal_id") in {p["id"] for p in reverted}]
        # replay: would the new prompt have proposed classic views for the undone ideas?
        ok_lines = len(lines) <= 12 and not FORBIDDEN.search(learned)
        replay_ok = ok_lines and new_nov < 0.5
        detail = (f"replayed {len(reverted)} undone proposals: all would now be classic views; "
                  f"learned part {len(lines)} lines, no rule overrides") if replay_ok else "replay failed"
        ver = {"agent": "designer", "version": max(v["version"] for v in self.prompts if v["agent"] == "designer") + 1,
               "core_hash": cur["core_hash"], "learned": learned, "diff": diff,
               "reason": f"The person undid {len(reverted)} invented view ideas in a row, so lean on classic views.",
               "evidence": evidence, "replay": {"passed": replay_ok, "detail": detail}, "active": replay_ok,
               "_novelty": new_nov}
        if replay_ok:
            for v in self.prompts:
                if v["agent"] == "designer":
                    v["active"] = False
            self.meta_state["novelty"] = new_nov
        self.prompts.append(ver)
        for p in reverted:
            p["_meta_used"] = True
        self.act("meta-learner", "prompt_rewrite",
                 f"designer v{ver['version']}: novelty {old_nov} -> {self.meta_state['novelty']}. Diff:\n{diff}")
        self.update_beliefs()
        return ver

    def _learned_line(self, n):
        fallback = f"- Prefer classic views (month, week, workweek, day): the person undid {n} invented layouts."
        text = llm.chat([{"role": "system", "content": "Write ONE short bullet line (under 140 characters, starting "
                          "with '- ') of guidance for a calendar view designer, based on this observation. "
                          "Plain words, no quotes."},
                         {"role": "user", "content": f"The person undid {n} invented calendar layouts in a row; "
                          "they seem to prefer classic views (month, week, workweek, day)."}])
        if text:
            line = text.strip().splitlines()[0].strip()
            if not line.startswith("- "):
                line = "- " + line.lstrip("-* ")
            if 10 < len(line) <= 160 and not FORBIDDEN.search(line):
                return line
        return fallback

    def rollback(self, agent):
        vers = [v for v in self.prompts if v["agent"] == agent]
        if not vers:
            return None
        cur = self.active_prompt(agent)
        prev = [v for v in vers if v["version"] < cur["version"] and v["replay"]["passed"]]
        if not prev:
            return self.pub(cur)
        target = prev[-1]
        for v in vers:
            v["active"] = v is target
        if agent == "designer":
            self.meta_state["novelty"] = target["_novelty"]
        self.act("meta-learner", "rollback", f"{agent}: v{cur['version']} -> v{target['version']} is active again")
        return self.pub(target)
