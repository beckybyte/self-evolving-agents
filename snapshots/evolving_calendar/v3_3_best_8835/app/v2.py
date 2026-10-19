import json
import re
import uuid
import hashlib
import copy
from datetime import datetime, date, time as dtime, timedelta

import v3

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ISO = "%Y-%m-%dT%H:%M"

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot",
          "lazy", "urgent", "alert", "dismiss", "relate", "later"]

KIND_WORDS = {
    "overdue": "waiting",
    "fail": "not work out",
    "miss": "skip",
    "late": "after",
    "behind": "after",
    "should have": "could",
    "forgot": "let slip",
    "lazy": "tired",
    "urgent": "pressing",
    "alert": "note",
    "dismiss": "set aside",
    "relate": "connect",
    "later": "soon",
}


def _clean(text):
    if not isinstance(text, str):
        return text
    out = text
    for bad in BANNED:
        if bad in out.lower():
            # replace case-insensitively
            pattern = re.compile(re.escape(bad), re.IGNORECASE)
            out = pattern.sub(KIND_WORDS.get(bad, "kind"), out)
    return out


def _now(S):
    return S.get("now") or "2026-10-19T20:00"


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, ISO)
    except Exception:
        return None


def _fmt_iso(dt):
    if dt is None:
        return None
    return dt.strftime(ISO)


def _new_id(prefix):
    return prefix + uuid.uuid4().hex[:10]


def _msg(S, kind, text, item_id=None, undo_id=None, steps=None):
    m = {
        "id": _new_id("m_"),
        "role": "agent",
        "kind": kind,
        "text": _clean(text),
        "item_id": item_id,
        "at": _now(S),
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        m["steps"] = steps or []
    S["messages"].append(m)
    return m


def _find_item(S, item_id):
    for it in S.get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def _beliefs_list(S):
    return [copy.deepcopy(b) for b in S.get("beliefs", {}).values()]


def _activity(S, agent, kind, text, **extra):
    ev = {"at": _now(S), "agent": agent, "kind": kind, "text": _clean(text)}
    for k, v in extra.items():
        ev[k] = v
    S.setdefault("activity", []).append(ev)
    return ev


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

CORE_TEXTS = {
    "designer": "Design calm views that never hide an item and always count what is left out.",
    "followthrough": "Move tasks forward quietly, one gentle question at a time, with no guilt.",
    "learner": "Learn only from evidence, keep beliefs small and plain, and never invent numbers.",
}


def _core_hash(agent):
    return hashlib.sha256(CORE_TEXTS.get(agent, agent).encode("utf-8")).hexdigest()[:12]


def init_v2(S):
    S["log"] = []
    S["beliefs"] = {}
    S["deleted_beliefs"] = []
    S["penalty"] = {}
    S["blocked"] = {}
    S["proposals"] = []
    S["plans"] = {}
    S["view"] = {"type": "week", "params": {}}
    S["meta"] = {
        "novelty": 0.6,
        "cadence_hours": 24,
        "breakdown_appetite": 0.5,
        "tone": "gentle",
        "threshold": 0.7,
    }
    S["invented_reverts"] = []
    S["last_proposal_at"] = None
    S["learn_mark"] = 0
    S["activity"] = []
    S["dismiss_streak"] = 0
    prompts = []
    for agent in ("designer", "followthrough", "learner"):
        prompts.append({
            "agent": agent,
            "version": 1,
            "core_hash": _core_hash(agent),
            "learned": "",
            "diff": "",
            "reason": "initial version",
            "evidence": [],
            "replay": {"passed": True, "detail": "initial"},
            "active": True,
        })
    S["prompts"] = prompts
    return {"ok": True}


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------

def add_log(S, kind, item_ids, payload, source):
    entry = {
        "id": _new_id("log_"),
        "at": _now(S),
        "kind": kind,
        "item_ids": list(item_ids or []),
        "payload": payload or {},
        "source": source,
    }
    S.setdefault("log", []).append(entry)
    return copy.deepcopy(entry)


def get_log(S):
    return {"entries": [copy.deepcopy(e) for e in S.get("log", [])]}


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

def _item_date(it):
    st = _parse_iso(it.get("start"))
    if st is None:
        return None
    return st.date()


def _hidden_for(S, keep):
    """keep: function(date_d) -> bool. Returns hidden_counts dict."""
    counts = {}
    for it in S.get("items", []):
        d = _item_date(it)
        if d is None:
            continue
        if not keep(d):
            key = d.strftime("%Y-%m-%d")
            counts[key] = counts.get(key, 0) + 1
    return counts


def view_state(S):
    view = S.get("view") or {"type": "week", "params": {}}
    vtype = view.get("type", "week")
    params = copy.deepcopy(view.get("params", {}))
    now_dt = _parse_iso(_now(S)) or datetime(2026, 10, 19, 20, 0)
    today_d = now_dt.date()
    out = {"type": vtype, "params": params}
    if vtype == "custom" and view.get("spec"):
        spec = view.get("spec")
        out["spec"] = copy.deepcopy(spec)
        try:
            out["hidden_counts"] = v3.hidden_counts(S, spec)
        except Exception:
            out["hidden_counts"] = {}
        return out
    if vtype == "workweek":
        hidden = _hidden_for(S, lambda d: d.weekday() < 5)
    elif vtype == "day":
        hidden = _hidden_for(S, lambda d: d == today_d)
    elif vtype == "week":
        monday = today_d - timedelta(days=today_d.weekday())
        sunday = monday + timedelta(days=6)
        hidden = _hidden_for(S, lambda d: monday <= d <= sunday)
    elif vtype == "month":
        hidden = _hidden_for(S, lambda d: d.year == today_d.year and d.month == today_d.month)
    else:
        hidden = {}
    out["hidden_counts"] = hidden
    return out


def set_view(S, view_type):
    if view_type not in ("month", "week", "workweek", "day"):
        raise KeyError("view type")
    S["view"] = {"type": view_type, "params": {}}
    add_log(S, "view_switch", [], {"type": view_type}, "person")
    return {"view": view_state(S)}


def meta_state(S):
    meta = S.get("meta", {})
    return {
        "novelty": meta.get("novelty", 0.6),
        "cadence_hours": meta.get("cadence_hours", 24),
        "breakdown_appetite": meta.get("breakdown_appetite", 0.5),
        "tone": meta.get("tone", "gentle"),
        "threshold": meta.get("threshold", 0.7),
    }


# ---------------------------------------------------------------------------
# habit learner
# ---------------------------------------------------------------------------

def _history(S):
    """List of (datetime, evidence_id, item) for done work."""
    hist = []
    for it in S.get("items", []):
        if it.get("status") == "done" and it.get("source") == "seed":
            t = _parse_iso(it.get("end")) or _parse_iso(it.get("start"))
            if t is not None:
                hist.append((t, it.get("id"), it))
    for e in S.get("log", []):
        if e.get("kind") == "done":
            t = _parse_iso(e.get("at"))
            if t is not None:
                hist.append((t, e.get("id"), None))
    return hist


def _conf(share, n, key, S):
    base = share * min(1.0, n / 6.0)
    pen = S.get("penalty", {}).get(key, 0.0)
    return round(max(0.0, base - pen), 2)


def _store_belief(S, key, level, statement, value, confidence, evidence):
    if not evidence:
        return None
    old = S["beliefs"].get(key)
    b = {
        "key": key,
        "level": level,
        "statement": _clean(statement),
        "value": value,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "evidence": list(evidence)[:20],
        "updated_at": _now(S),
    }
    S["beliefs"][key] = b
    changed = old is None or abs(old.get("confidence", 0) - b["confidence"]) >= 0.05
    if changed:
        _activity(S, "learner", "belief_update",
                  "%s (confidence %.2f)" % (b["statement"], b["confidence"]))
    if old is not None and old.get("confidence", 0) < 0.7 <= b["confidence"]:
        _activity(S, "learner", "threshold_crossed",
                  "%s now rests on enough evidence (%.2f)." % (b["statement"], b["confidence"]))
    return b


def learn_beliefs(S):
    deleted = set(S.get("deleted_beliefs", []))
    hist = _history(S)
    n = len(hist)

    # evening_work / morning_work
    if "evening_work" not in deleted or "morning_work" not in deleted:
        if n >= 5:
            evening = [h for h in hist if h[0].hour >= 20]
            morning = [h for h in hist if h[0].hour < 12]
            k_e = len(evening)
            k_m = len(morning)
            if k_e / n >= 0.5 and "evening_work" not in deleted:
                ev = [h[1] for h in evening][:20]
                _store_belief(S, "evening_work", "habit",
                              "Finishes tasks after 8 PM (%d of %d)" % (k_e, n),
                              20, _conf(k_e / n, n, "evening_work", S), ev)
            elif k_m / n >= 0.5 and "morning_work" not in deleted:
                ev = [h[1] for h in morning][:20]
                _store_belief(S, "morning_work", "habit",
                              "Gets things done in the morning (%d of %d)" % (k_m, n),
                              12, _conf(k_m / n, n, "morning_work", S), ev)

    # no_weekend
    if "no_weekend" not in deleted:
        dated = []
        for it in S.get("items", []):
            d = _item_date(it)
            if d is not None:
                dated.append((d, it.get("id")))
        weeks = set()
        weekend = 0
        for d, _iid in dated:
            weeks.add(d.isocalendar()[:2])
            if d.weekday() >= 5:
                weekend += 1
        w = len(weeks)
        if w >= 4 and weekend == 0 and dated:
            ev = [iid for _d, iid in dated][:20]
            conf = min(1.0, w / 8.0) - S.get("penalty", {}).get("no_weekend", 0.0)
            _store_belief(S, "no_weekend", "habit",
                          "No weekend plans in %d weeks (0 of %d items)" % (w, len(dated)),
                          True, conf, ev)

    # block_length
    if "block_length" not in deleted:
        lens = []
        for t, iid, it in hist:
            if it is None:
                continue
            st = _parse_iso(it.get("start"))
            en = _parse_iso(it.get("end"))
            if st is not None and en is not None:
                lens.append(((en - st).total_seconds() / 60.0, iid))
        if len(lens) >= 6:
            vals = sorted(x[0] for x in lens)
            m = vals[len(vals) // 2]
            k = sum(1 for v, _ in lens if abs(v - m) <= 15)
            ev = [iid for _v, iid in lens][:20]
            _store_belief(S, "block_length", "habit",
                          "Tasks usually take about %d minutes (%d of %d)" % (int(m), k, len(lens)),
                          int(m), _conf(k / len(lens), len(lens), "block_length", S), ev)
    return S["beliefs"]


def get_beliefs(S):
    return {"beliefs": _beliefs_list(S)}


def delete_belief(S, key):
    if key in S.get("beliefs", {}):
        del S["beliefs"][key]
    if key not in S.get("deleted_beliefs", []):
        S.setdefault("deleted_beliefs", []).append(key)
    add_log(S, "belief_deleted", [], {"key": key}, "person")
    _activity(S, "learner", "belief_deleted", "Set aside the belief '%s'." % key)
    return {"ok": True}


# ---------------------------------------------------------------------------
# proposal gate
# ---------------------------------------------------------------------------

ORDER = ["no_weekend", "evening_work", "morning_work", "block_length"]


def _open_proposal(S):
    for p in S.get("proposals", []):
        if p.get("status") == "shown":
            return p
    return None


def _classic_view(S, key, belief):
    if key == "no_weekend":
        return {"type": "workweek", "params": {}}
    if key == "evening_work":
        return {"type": "day", "params": {"focus": "evening"}}
    if key == "morning_work":
        return {"type": "day", "params": {"focus": "morning"}}
    if key == "block_length":
        return {"type": "week", "params": {"block_min": belief.get("value")}}
    return {"type": "week", "params": {}}


def _titles(key):
    return {
        "no_weekend": "Hide weekends",
        "evening_work": "Evening lane",
        "morning_work": "Morning lane",
        "block_length": "Block sizes",
    }.get(key, "A calmer view")


def _prompt_versions(S):
    out = {}
    for p in S.get("prompts", []):
        if p.get("active"):
            out[p["agent"]] = p["version"]
    return out


def run_gate(S):
    if _open_proposal(S) is not None:
        return None
    now_dt = _parse_iso(_now(S))
    last = S.get("last_proposal_at")
    cadence = S.get("meta", {}).get("cadence_hours", 24)
    if last is not None:
        last_dt = _parse_iso(last)
        if last_dt is not None and now_dt is not None:
            if (now_dt - last_dt) <= timedelta(hours=cadence):
                return None
    threshold = S.get("meta", {}).get("threshold", 0.7)
    blocked = S.get("blocked", {})
    cands = []
    for key, b in S.get("beliefs", {}).items():
        if b.get("level") != "habit":
            continue
        conf = b.get("confidence", 0)
        if conf < threshold:
            continue
        if key in blocked and conf <= blocked[key] + 0.15:
            continue
        cands.append((key, b))
    if not cands:
        return None
    cands.sort(key=lambda kb: (ORDER.index(kb[0]) if kb[0] in ORDER else 99,
                               -kb[1].get("confidence", 0)))
    key, belief = cands[0]
    invented = len(S.get("invented_reverts", [])) < 2
    if invented:
        try:
            spec = v3.best_spec(S, key)
        except Exception:
            spec = None
        if spec:
            view = {"type": "custom", "params": {"belief": key}, "spec": spec,
                    "hidden_counts": v3.hidden_counts(S, spec)}
        else:
            view = _classic_view(S, key, belief)
            invented = False
    else:
        view = _classic_view(S, key, belief)
    novelty = "invented" if invented else "classic"
    reason = "%s. %s?" % (belief.get("statement", ""),
                          "Try a calmer view" if invented else _titles(key))
    prop = {
        "id": _new_id("prop_"),
        "title": _titles(key),
        "reason": _clean(reason),
        "view": view,
        "belief_keys": [key],
        "novelty": novelty,
        "prompt_versions": _prompt_versions(S),
        "status": "shown",
    }
    S.setdefault("proposals", []).append(prop)
    S["last_proposal_at"] = _now(S)
    add_log(S, "proposal_shown", [], {"proposal_id": prop["id"], "belief": key}, "agent")
    _activity(S, "designer", "proposal_shown",
              "An idea: %s. %s" % (prop["title"], prop["reason"]))
    return prop


def get_proposal(S):
    p = _open_proposal(S)
    return {"proposal": copy.deepcopy(p) if p else None}


# ---------------------------------------------------------------------------
# meta learner
# ---------------------------------------------------------------------------

def _check_learned(text):
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) > 12:
        return False, "learned text has more than 12 lines"
    low = text.lower()
    for bad in ("ignore", "override", "core rules"):
        if bad in low:
            return False, "learned text tries to override the core rules"
    return True, "learned text checked: <= 12 lines, no rule override"


def _meta_learn(S):
    active = None
    for p in S.get("prompts", []):
        if p.get("agent") == "designer" and p.get("active"):
            active = p
    if active is None:
        return None
    line = "- Prefer classic views (workweek, week, day): 2 invented views were reverted."
    prev_lines = [ln for ln in active.get("learned", "").split("\n") if ln.strip()]
    if line in prev_lines:
        return None
    new_lines = prev_lines + [line]
    if len(new_lines) > 12:
        new_lines = new_lines[-12:]
    learned = "\n".join(new_lines)
    passed, detail = _check_learned(learned)
    evidence = list(S.get("invented_reverts", []))[-20:]
    new_version = {
        "agent": "designer",
        "version": active.get("version", 1) + 1,
        "core_hash": active.get("core_hash"),
        "learned": learned,
        "diff": "+ " + line,
        "reason": "2 invented proposals were reverted",
        "evidence": evidence,
        "replay": {"passed": passed, "detail": detail},
        "active": True,
    }
    active["active"] = False
    S.setdefault("prompts", []).append(new_version)
    S["beliefs"]["prefers_classic"] = {
        "key": "prefers_classic",
        "level": "meta",
        "statement": "Prefers classic views (2 of 2 invented views reverted)",
        "value": "classic",
        "confidence": 0.8,
        "evidence": evidence[:20],
        "updated_at": _now(S),
    }
    _activity(S, "designer", "prompt_rewrite",
              "designer v%d: %s" % (new_version["version"], new_version["diff"]),
              diff=new_version["diff"])
    return new_version


def rollback(S, agent):
    versions = [p for p in S.get("prompts", []) if p.get("agent") == agent]
    if not versions:
        raise KeyError("agent")
    active = None
    for p in versions:
        if p.get("active"):
            active = p
    if active is None:
        active = versions[-1]
        active["active"] = True
        return {"active": copy.deepcopy(active)}
    idx = versions.index(active)
    if idx == 0:
        return {"active": copy.deepcopy(active)}
    prev = versions[idx - 1]
    active["active"] = False
    prev["active"] = True
    _activity(S, "designer", "prompt_rollback",
              "%s rolled back to v%d." % (agent, prev.get("version", 1)))
    return {"active": copy.deepcopy(prev)}


def get_prompts(S):
    return {"prompts": [copy.deepcopy(p) for p in S.get("prompts", [])]}


def get_activity(S):
    return {"events": [copy.deepcopy(e) for e in S.get("activity", [])]}


# ---------------------------------------------------------------------------
# learn / tick
# ---------------------------------------------------------------------------

def learn(S):
    learn_beliefs(S)
    prop = run_gate(S)
    return {"beliefs": _beliefs_list(S), "proposal": copy.deepcopy(prop) if prop else None}


def _recompute_belief(S, key):
    """Recompute a single belief's confidence after a penalty change."""
    b = S["beliefs"].get(key)
    if not b:
        return
    stmt = b.get("statement", "")
    m = re.search(r"\((\d+) of (\d+)", stmt)
    if m:
        k = int(m.group(1))
        n = int(m.group(2))
        if n > 0:
            share = k / n
            b["confidence"] = _conf(share, n, key, S)
            return
    m2 = re.search(r"in (\d+) weeks", stmt)
    if m2:
        w = int(m2.group(1))
        conf = min(1.0, w / 8.0) - S.get("penalty", {}).get(key, 0.0)
        b["confidence"] = round(max(0.0, conf), 2)
        return
    b["confidence"] = round(max(0.0, b.get("confidence", 0) - 0.2), 2)


def proposal_action(S, proposal_id, action):
    prop = None
    for p in S.get("proposals", []):
        if p.get("id") == proposal_id:
            prop = p
    if prop is None:
        raise KeyError("proposal")
    key = (prop.get("belief_keys") or [None])[0]
    if action == "accept" and prop.get("status") == "shown":
        prop["prev_view"] = copy.deepcopy(S.get("view"))
        S["view"] = copy.deepcopy(prop.get("view"))
        prop["status"] = "accepted"
        prop["accepted_at"] = _now(S)
        prop["kept_credit"] = False
        S["dismiss_streak"] = 0
        add_log(S, "proposal_accepted", [], {"proposal_id": prop["id"]}, "person")
        _activity(S, "designer", "proposal_accepted",
                  "You tried '%s'." % prop.get("title", "the view"))
    elif action == "dismiss" and prop.get("status") == "shown":
        prop["status"] = "dismissed"
        if key and key in S.get("beliefs", {}):
            S["blocked"][key] = S["beliefs"][key].get("confidence", 0)
        add_log(S, "proposal_dismissed", [], {"proposal_id": prop["id"]}, "person")
        _activity(S, "designer", "proposal_dismissed",
                  "Set aside the idea '%s'." % prop.get("title", "the view"))
        S["dismiss_streak"] = S.get("dismiss_streak", 0) + 1
        if S["dismiss_streak"] >= 3:
            meta = S["meta"]
            old_t = meta.get("threshold", 0.7)
            old_c = meta.get("cadence_hours", 24)
            meta["threshold"] = round(min(0.95, old_t + 0.1), 2)
            meta["cadence_hours"] = old_c * 2
            S["dismiss_streak"] = 0
            _activity(S, "designer", "knob_change",
                      "Raised the proposal threshold from %.2f to %.2f and cadence from %s to %s hours: 3 proposals in a row were turned down."
                      % (old_t, meta["threshold"], _fmt_num(old_c), _fmt_num(meta["cadence_hours"])))
    elif action == "undo" and prop.get("status") == "accepted":
        S["view"] = copy.deepcopy(prop.get("prev_view") or {"type": "week", "params": {}})
        prop["status"] = "reverted"
        if key:
            S["penalty"][key] = S.get("penalty", {}).get(key, 0.0) + 0.2
            _recompute_belief(S, key)
            if key in S.get("beliefs", {}):
                S["blocked"][key] = S["beliefs"][key].get("confidence", 0)
        log = add_log(S, "proposal_reverted", [], {"proposal_id": prop["id"]}, "person")
        _activity(S, "designer", "proposal_reverted",
                  "You went back from '%s'." % prop.get("title", "the view"))
        if prop.get("novelty") == "invented":
            S.setdefault("invented_reverts", []).append(log["id"])
            meta = S["meta"]
            meta["novelty"] = round(max(0.0, meta.get("novelty", 0.6) - 0.1), 2)
            if len(S["invented_reverts"]) in (2, 4, 6, 8):
                _meta_learn(S)
    else:
        raise KeyError("action")
    return {"proposal": copy.deepcopy(prop), "view": view_state(S)}


def _fmt_num(x):
    if isinstance(x, float) and x == int(x):
        return str(int(x))
    return str(x)


def tick(S, on_clock):
    if on_clock:
        now_dt = _parse_iso(_now(S))
        for p in S.get("proposals", []):
            if p.get("status") == "accepted" and not p.get("kept_credit"):
                acc = _parse_iso(p.get("accepted_at"))
                if acc is not None and now_dt is not None and (now_dt - acc) >= timedelta(hours=72):
                    p["kept_credit"] = True
                    meta = S["meta"]
                    old_t = meta.get("threshold", 0.7)
                    meta["threshold"] = round(max(0.5, old_t - 0.05), 2)
                    _activity(S, "designer", "knob_change",
                              "Lowered the proposal threshold from %.2f to %.2f: you kept the view '%s' for 3 days."
                              % (old_t, meta["threshold"], p.get("title", "the view")))
    if on_clock or (len(S.get("log", [])) - S.get("learn_mark", 0)) >= 5:
        S["learn_mark"] = len(S.get("log", []))
        learn_beliefs(S)
        run_gate(S)
    return None
