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

def _now(S):
    try:
        return datetime.strptime(S.get("now") or "2026-10-19T20:00", "%Y-%m-%dT%H:%M")
    except Exception:
        return datetime(2026, 10, 19, 20, 0)


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except Exception:
        return None


def _fmt_iso(dt):
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M")


def _new_id(prefix):
    return prefix + uuid.uuid4().hex[:10]


_BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot",
           "lazy", "urgent", "alert", "dismiss", "relate", "later"]


def _clean(text):
    if not isinstance(text, str):
        return text
    out = text
    low = out.lower()
    for bad in _BANNED:
        if bad in low:
            # replace every occurrence case-insensitively with a kind phrase
            out = re.sub(re.escape(bad), "moved", out, flags=re.IGNORECASE)
            low = out.lower()
    return out


def _msg(S, kind, text, item_id=None, undo_id=None, steps=None):
    m = {
        "id": _new_id("m_"),
        "role": "agent",
        "kind": kind,
        "text": _clean(text),
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        m["steps"] = steps or []
    S.setdefault("messages", []).append(m)
    return m


def _activity(S, agent, kind, text, extra=None):
    ev = {"at": S.get("now"), "agent": agent, "kind": kind, "text": _clean(text)}
    if extra:
        for k, v in extra.items():
            ev[k] = v
    S.setdefault("activity", []).append(ev)
    return ev


def _find_item(S, item_id):
    for it in S.get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def _item_time(it):
    """time used for history: end else start"""
    return _parse_iso(it.get("end")) or _parse_iso(it.get("start"))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

_CORE_TEXTS = {
    "designer": "Design calm views that never hide an item and always count what they leave out.",
    "followthrough": "Move tasks forward quietly with kind wording and one gentle question at a time.",
    "learner": "Learn habits only from evidence and keep every belief plain and deletable.",
}


def _core_hash(agent):
    return hashlib.sha256(_CORE_TEXTS.get(agent, agent).encode("utf-8")).hexdigest()[:12]


def _initial_prompt(agent):
    return {
        "agent": agent,
        "version": 1,
        "core_hash": _core_hash(agent),
        "learned": "",
        "diff": "",
        "reason": "initial version",
        "evidence": [],
        "replay": {"passed": True, "detail": "initial"},
        "active": True,
    }


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
    S["prompts"] = [_initial_prompt("designer"),
                    _initial_prompt("followthrough"),
                    _initial_prompt("learner")]
    S["dismiss_streak"] = 0
    return S


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------

def add_log(S, kind, item_ids, payload, source):
    entry = {
        "id": _new_id("log_"),
        "at": S.get("now"),
        "kind": kind,
        "item_ids": list(item_ids or []),
        "payload": payload or {},
        "source": source,
    }
    S.setdefault("log", []).append(entry)
    return entry


def get_log(S):
    return {"entries": [copy.deepcopy(e) for e in S.get("log", [])]}


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

def _hidden_for_type(S, vtype, params):
    items = [it for it in S.get("items", []) if it.get("start")]
    now = _now(S)
    today_d = now.date()
    hidden = {}
    for it in items:
        st = _parse_iso(it.get("start"))
        if st is None:
            continue
        d = st.date()
        hide = False
        if vtype == "workweek":
            if d.weekday() >= 5:
                hide = True
        elif vtype == "day":
            if d != today_d:
                hide = True
        elif vtype == "week":
            monday = today_d - timedelta(days=today_d.weekday())
            sunday = monday + timedelta(days=6)
            if d < monday or d > sunday:
                hide = True
        elif vtype == "month":
            if d.year != today_d.year or d.month != today_d.month:
                hide = True
        elif vtype == "custom":
            if params and params.get("hide_weekends") and d.weekday() >= 5:
                hide = True
        if hide:
            key = d.strftime("%Y-%m-%d")
            hidden[key] = hidden.get(key, 0) + 1
    return hidden


def view_state(S):
    view = S.get("view") or {"type": "week", "params": {}}
    vtype = view.get("type", "week")
    params = view.get("params", {}) or {}
    out = {"type": vtype, "params": copy.deepcopy(params)}
    if vtype == "custom" and view.get("spec"):
        out["spec"] = copy.deepcopy(view["spec"])
        try:
            out["hidden_counts"] = v3.hidden_counts(S, view["spec"])
        except Exception:
            out["hidden_counts"] = {}
    else:
        out["hidden_counts"] = _hidden_for_type(S, vtype, params)
    return out


def set_view(S, view_type):
    if view_type not in ("month", "week", "workweek", "day"):
        raise KeyError("view")
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
    """list of (time_dt, evidence_id, item) for done history"""
    hist = []
    for it in S.get("items", []):
        if it.get("status") == "done":
            t = _item_time(it)
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
    pen = S.get("penalty", {}).get(key, 0)
    return round(max(0.0, base - pen), 2)


def _store_belief(S, key, level, statement, value, confidence, evidence):
    if not evidence:
        return None
    b = {
        "key": key,
        "level": level,
        "statement": _clean(statement),
        "value": value,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "evidence": list(evidence)[:20],
        "updated_at": S.get("now"),
    }
    return b


def learn_beliefs(S):
    now = _now(S)
    deleted = set(S.get("deleted_beliefs", []))
    old = S.get("beliefs", {})
    new_beliefs = {}

    hist = _history(S)
    n = len(hist)

    # evening_work / morning_work
    if n >= 5:
        evening = [h for h in hist if h[0].hour >= 20]
        morning = [h for h in hist if h[0].hour < 12]
        k_even = len(evening)
        k_morn = len(morning)
        if k_even / n >= 0.5:
            key = "evening_work"
            if key not in deleted:
                share = k_even / n
                conf = _conf(share, n, key, S)
                ev = [h[1] for h in evening][:20]
                b = _store_belief(S, key, "habit",
                                  "Finishes tasks after 8 PM (%d of %d)" % (k_even, n),
                                  20, conf, ev)
                if b:
                    new_beliefs[key] = b
        elif k_morn / n >= 0.5:
            key = "morning_work"
            if key not in deleted:
                share = k_morn / n
                conf = _conf(share, n, key, S)
                ev = [h[1] for h in morning][:20]
                b = _store_belief(S, key, "habit",
                                  "Gets things done in the morning (%d of %d)" % (k_morn, n),
                                  12, conf, ev)
                if b:
                    new_beliefs[key] = b

    # no_weekend
    key = "no_weekend"
    if key not in deleted:
        started = [it for it in S.get("items", []) if it.get("start")]
        weeks = set()
        weekend = 0
        ev = []
        for it in started:
            st = _parse_iso(it.get("start"))
            if st is None:
                continue
            iso = st.isocalendar()
            weeks.add((iso[0], iso[1]))
            if st.weekday() >= 5:
                weekend += 1
            ev.append(it.get("id"))
        w = len(weeks)
        if w >= 4 and weekend == 0 and started:
            share = 1.0
            conf = round(max(0.0, min(1.0, w / 8.0) - S.get("penalty", {}).get(key, 0)), 2)
            b = _store_belief(S, key, "habit",
                              "No weekend plans in %d weeks (0 of %d items)" % (w, len(started)),
                              True, conf, ev)
            if b:
                new_beliefs[key] = b

    # block_length
    key = "block_length"
    if key not in deleted:
        lengths = []
        ev = []
        for it in S.get("items", []):
            if it.get("status") != "done":
                continue
            st = _parse_iso(it.get("start"))
            en = _parse_iso(it.get("end"))
            if st and en and en > st:
                lengths.append(int((en - st).total_seconds() // 60))
                ev.append(it.get("id"))
        if len(lengths) >= 6:
            srt = sorted(lengths)
            m = srt[len(srt) // 2]
            k = len([x for x in lengths if abs(x - m) <= 15])
            share = k / len(lengths)
            conf = _conf(share, len(lengths), key, S)
            b = _store_belief(S, key, "habit",
                              "Tasks usually take about %d minutes (%d of %d)" % (m, k, len(lengths)),
                              m, conf, ev)
            if b:
                new_beliefs[key] = b

    # keep meta beliefs (prefers_classic) untouched
    for k, b in old.items():
        if b.get("level") == "meta" and k not in new_beliefs:
            new_beliefs[k] = b

    # activity for changes
    for k, b in new_beliefs.items():
        prev = old.get(k)
        if prev is None:
            _activity(S, "learner", "belief_update",
                      "%s (confidence %.2f)" % (b["statement"], b["confidence"]))
            if b["confidence"] >= 0.7:
                _activity(S, "learner", "threshold_crossed",
                          "%s reached %.2f" % (b["statement"], b["confidence"]))
        else:
            if abs(prev.get("confidence", 0) - b["confidence"]) >= 0.05:
                _activity(S, "learner", "belief_update",
                          "%s (confidence %.2f)" % (b["statement"], b["confidence"]))
            if prev.get("confidence", 0) < 0.7 <= b["confidence"]:
                _activity(S, "learner", "threshold_crossed",
                          "%s reached %.2f" % (b["statement"], b["confidence"]))

    S["beliefs"] = new_beliefs
    return new_beliefs


def get_beliefs(S):
    return {"beliefs": [copy.deepcopy(b) for b in S.get("beliefs", {}).values()]}


def delete_belief(S, key):
    beliefs = S.get("beliefs", {})
    if key in beliefs:
        del beliefs[key]
    if key not in S.get("deleted_beliefs", []):
        S.setdefault("deleted_beliefs", []).append(key)
    add_log(S, "belief_deleted", [], {"key": key}, "person")
    _activity(S, "learner", "belief_deleted", "Removed the belief '%s'." % key)
    return {"ok": True}


# ---------------------------------------------------------------------------
# proposal gate
# ---------------------------------------------------------------------------

_ORDER = ["no_weekend", "evening_work", "morning_work", "block_length"]


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


def _title_for(key):
    return {
        "no_weekend": "Hide weekends",
        "evening_work": "Evening lane",
        "morning_work": "Morning lane",
        "block_length": "Block length view",
    }.get(key, "A new view")


def _active_prompt_versions(S):
    out = {}
    for p in S.get("prompts", []):
        if p.get("active"):
            out[p["agent"]] = p["version"]
    return out


def run_gate(S):
    now = _now(S)
    proposals = S.get("proposals", [])
    open_prop = None
    for p in proposals:
        if p.get("status") == "shown":
            open_prop = p
            break
    if open_prop is not None:
        return open_prop

    cadence = S.get("meta", {}).get("cadence_hours", 24)
    last = S.get("last_proposal_at")
    if last is not None:
        last_dt = _parse_iso(last)
        if last_dt is not None and (now - last_dt) <= timedelta(hours=cadence):
            return None

    threshold = S.get("meta", {}).get("threshold", 0.7)
    blocked = S.get("blocked", {})
    candidates = []
    for key, b in S.get("beliefs", {}).items():
        if b.get("level") != "habit":
            continue
        conf = b.get("confidence", 0)
        if conf < threshold:
            continue
        if key in blocked and conf <= blocked[key] + 0.15:
            continue
        candidates.append((key, b))
    if not candidates:
        return None

    def sort_key(pair):
        key, b = pair
        idx = _ORDER.index(key) if key in _ORDER else len(_ORDER)
        return (idx, -b.get("confidence", 0))

    candidates.sort(key=sort_key)
    key, belief = candidates[0]

    novelty = "invented" if len(S.get("invented_reverts", [])) < 2 else "classic"
    if novelty == "invented":
        try:
            spec = v3.best_spec(S, key)
        except Exception:
            spec = None
        if spec:
            try:
                hidden = v3.hidden_counts(S, spec)
            except Exception:
                hidden = {}
            view = {"type": "custom", "params": {"belief": key}, "spec": spec,
                    "hidden_counts": hidden}
        else:
            view = _classic_view(S, key, belief)
            view["hidden_counts"] = _hidden_for_type(S, view["type"], view.get("params", {}))
    else:
        view = _classic_view(S, key, belief)
        view["hidden_counts"] = _hidden_for_type(S, view["type"], view.get("params", {}))

    statement = belief.get("statement", "")
    reason = statement + " " + _title_for(key) + "?"
    prop = {
        "id": _new_id("prop_"),
        "title": _title_for(key),
        "reason": _clean(reason),
        "view": view,
        "belief_keys": [key],
        "novelty": novelty,
        "prompt_versions": _active_prompt_versions(S),
        "status": "shown",
    }
    proposals.append(prop)
    S["last_proposal_at"] = S.get("now")
    add_log(S, "proposal_shown", [], {"proposal_id": prop["id"], "belief": key}, "agent")
    _activity(S, "designer", "proposal_shown",
              "An idea: %s (%s)." % (prop["title"], statement))
    return prop


def _open_proposal(S):
    for p in S.get("proposals", []):
        if p.get("status") == "shown":
            return p
    return None


def get_proposal(S):
    p = _open_proposal(S)
    return {"proposal": copy.deepcopy(p) if p else None}


def _recompute_belief(S, key):
    b = S.get("beliefs", {}).get(key)
    if not b:
        return None
    pen = S.get("penalty", {}).get(key, 0)
    # recompute from stored share proxy: use current confidence minus old penalty
    # simpler: subtract penalty from a base stored in value? We recompute from evidence.
    hist = _history(S)
    n = len(hist)
    if key == "evening_work" and n:
        k = len([h for h in hist if h[0].hour >= 20])
        share = k / n
        b["confidence"] = _conf(share, n, key, S)
    elif key == "morning_work" and n:
        k = len([h for h in hist if h[0].hour < 12])
        share = k / n
        b["confidence"] = _conf(share, n, key, S)
    elif key == "no_weekend":
        started = [it for it in S.get("items", []) if it.get("start")]
        weeks = set()
        for it in started:
            st = _parse_iso(it.get("start"))
            if st:
                iso = st.isocalendar()
                weeks.add((iso[0], iso[1]))
        w = len(weeks)
        b["confidence"] = round(max(0.0, min(1.0, w / 8.0) - pen), 2)
    elif key == "block_length":
        lengths = []
        for it in S.get("items", []):
            if it.get("status") != "done":
                continue
            st = _parse_iso(it.get("start"))
            en = _parse_iso(it.get("end"))
            if st and en and en > st:
                lengths.append(int((en - st).total_seconds() // 60))
        if lengths:
            srt = sorted(lengths)
            m = srt[len(srt) // 2]
            k = len([x for x in lengths if abs(x - m) <= 15])
            b["confidence"] = _conf(k / len(lengths), len(lengths), key, S)
    else:
        b["confidence"] = round(max(0.0, b.get("confidence", 0) - pen), 2)
    b["updated_at"] = S.get("now")
    return b


def proposal_action(S, proposal_id, action):
    prop = None
    for p in S.get("proposals", []):
        if p.get("id") == proposal_id:
            prop = p
            break
    if prop is None:
        raise KeyError("proposal")

    if action == "accept" and prop.get("status") == "shown":
        prop["prev_view"] = copy.deepcopy(S.get("view"))
        S["view"] = copy.deepcopy(prop.get("view"))
        prop["status"] = "accepted"
        prop["accepted_at"] = S.get("now")
        prop["kept_credit"] = False
        S["dismiss_streak"] = 0
        add_log(S, "proposal_accepted", [], {"proposal_id": prop["id"]}, "person")
        _activity(S, "designer", "proposal_accepted",
                  "You tried '%s'." % prop.get("title", "the view"))
    elif action == "dismiss" and prop.get("status") == "shown":
        prop["status"] = "dismissed"
        for key in prop.get("belief_keys", []):
            b = S.get("beliefs", {}).get(key)
            if b:
                S.setdefault("blocked", {})[key] = b.get("confidence", 0)
        add_log(S, "proposal_dismissed", [], {"proposal_id": prop["id"]}, "person")
        _activity(S, "designer", "proposal_dismissed",
                  "You set '%s' aside for now." % prop.get("title", "the view"))
        S["dismiss_streak"] = S.get("dismiss_streak", 0) + 1
        if S["dismiss_streak"] >= 3:
            meta = S.setdefault("meta", {})
            old_thr = meta.get("threshold", 0.7)
            old_cad = meta.get("cadence_hours", 24)
            new_thr = round(min(0.95, old_thr + 0.1), 2)
            new_cad = old_cad * 2
            meta["threshold"] = new_thr
            meta["cadence_hours"] = new_cad
            S["dismiss_streak"] = 0
            _activity(S, "designer", "knob_change",
                      "Raised the proposal threshold from %.2f to %.2f and cadence from %s to %s hours: 3 proposals in a row were turned down."
                      % (old_thr, new_thr, _num(old_cad), _num(new_cad)))
    elif action == "undo" and prop.get("status") == "accepted":
        prev = prop.get("prev_view") or {"type": "week", "params": {}}
        S["view"] = copy.deepcopy(prev)
        prop["status"] = "reverted"
        for key in prop.get("belief_keys", []):
            S.setdefault("penalty", {})[key] = S.get("penalty", {}).get(key, 0) + 0.2
            b = _recompute_belief(S, key)
            if b:
                S.setdefault("blocked", {})[key] = b.get("confidence", 0)
        log_entry = add_log(S, "proposal_reverted", [], {"proposal_id": prop["id"]}, "person")
        _activity(S, "designer", "proposal_reverted",
                  "You went back from '%s'." % prop.get("title", "the view"))
        if prop.get("novelty") == "invented":
            S.setdefault("invented_reverts", []).append(log_entry["id"])
            meta = S.setdefault("meta", {})
            meta["novelty"] = round(max(0.0, meta.get("novelty", 0.6) - 0.1), 2)
            n = len(S["invented_reverts"])
            if n >= 2 and n % 2 == 0:
                _meta_learn(S)
    else:
        # unknown action or wrong status: return current state unchanged
        pass

    return {"proposal": copy.deepcopy(prop), "view": view_state(S)}


def _num(x):
    if isinstance(x, float) and x == int(x):
        return int(x)
    return x


# ---------------------------------------------------------------------------
# meta learner
# ---------------------------------------------------------------------------

def _meta_learn(S):
    prompts = S.get("prompts", [])
    designer_versions = [p for p in prompts if p.get("agent") == "designer"]
    if not designer_versions:
        return
    active = None
    for p in designer_versions:
        if p.get("active"):
            active = p
    if active is None:
        active = designer_versions[-1]
    line = "- Prefer classic views (workweek, week, day): 2 invented views were reverted."
    prev_learned = active.get("learned", "") or ""
    lines = [ln for ln in prev_learned.split("\n") if ln.strip()]
    if line not in lines:
        lines.append(line)
    lines = lines[:12]
    learned = "\n".join(lines)
    # safety check
    low = learned.lower()
    for bad in ("ignore", "override", "core rules"):
        if bad in low:
            learned = prev_learned
            lines = [ln for ln in learned.split("\n") if ln.strip()]
    new_version = {
        "agent": "designer",
        "version": active.get("version", 1) + 1,
        "core_hash": active.get("core_hash", _core_hash("designer")),
        "learned": learned,
        "diff": "+ " + line,
        "reason": "2 invented proposals were reverted",
        "evidence": list(S.get("invented_reverts", []))[:20],
        "replay": {"passed": True,
                   "detail": "learned text checked: <= 12 lines, no rule override"},
        "active": True,
    }
    active["active"] = False
    prompts.append(new_version)
    S["prompts"] = prompts

    # meta belief
    ev = list(S.get("invented_reverts", []))[:20]
    if ev:
        S.setdefault("beliefs", {})["prefers_classic"] = {
            "key": "prefers_classic",
            "level": "meta",
            "statement": "Prefers classic views (2 of 2 invented views reverted)",
            "value": "classic",
            "confidence": 0.8,
            "evidence": ev,
            "updated_at": S.get("now"),
        }
    _activity(S, "designer", "prompt_rewrite",
              "designer v%d: %s" % (new_version["version"], new_version["diff"]),
              {"diff": new_version["diff"]})


def get_prompts(S):
    return {"prompts": [copy.deepcopy(p) for p in S.get("prompts", [])]}


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
    if active.get("version", 1) <= 1:
        # keep v1 active
        return {"active": copy.deepcopy(active)}
    prev = None
    for p in versions:
        if p.get("version") == active.get("version", 1) - 1:
            prev = p
    if prev is None:
        return {"active": copy.deepcopy(active)}
    active["active"] = False
    prev["active"] = True
    _activity(S, "designer", "prompt_rollback",
              "%s rolled back to v%d." % (agent, prev.get("version", 1)))
    return {"active": copy.deepcopy(prev)}


# ---------------------------------------------------------------------------
# learn / tick
# ---------------------------------------------------------------------------

def learn(S):
    learn_beliefs(S)
    prop = run_gate(S)
    return {"beliefs": [copy.deepcopy(b) for b in S.get("beliefs", {}).values()],
            "proposal": copy.deepcopy(prop) if prop else None}


def tick(S, on_clock):
    now = _now(S)
    if on_clock:
        for p in S.get("proposals", []):
            if p.get("status") == "accepted" and not p.get("kept_credit"):
                acc = _parse_iso(p.get("accepted_at"))
                if acc is not None and (now - acc) >= timedelta(hours=72):
                    p["kept_credit"] = True
                    meta = S.setdefault("meta", {})
                    old_thr = meta.get("threshold", 0.7)
                    new_thr = round(max(0.5, old_thr - 0.05), 2)
                    meta["threshold"] = new_thr
                    _activity(S, "designer", "knob_change",
                              "Lowered the proposal threshold from %.2f to %.2f: you kept the view '%s' for 3 days."
                              % (old_thr, new_thr, p.get("title", "the view")))
    if on_clock or (len(S.get("log", [])) - S.get("learn_mark", 0)) >= 5:
        S["learn_mark"] = len(S.get("log", []))
        learn_beliefs(S)
        run_gate(S)


def get_activity(S):
    return {"events": [copy.deepcopy(e) for e in S.get("activity", [])]}
