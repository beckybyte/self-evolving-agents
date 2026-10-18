import json
import re
import copy
from datetime import datetime, date, time as dtime, timedelta

# === SECTION: catalog ===

CATALOG = [
    {"type": "MonthGrid", "props": {"month": "date"}, "children": False},
    {"type": "WeekColumns", "props": {"start": "date", "days": "int"}, "children": False},
    {"type": "DayTimeline", "props": {"date": "date", "from_hour": "int", "to_hour": "int"}, "children": False},
    {"type": "AgendaList", "props": {"from": "date", "to": "date"}, "children": False},
    {"type": "Lane", "props": {"label": "str", "filter": "filter"}, "children": True},
    {"type": "Bucket", "props": {"label": "str", "filter": "filter"}, "children": False},
]

CATALOG_BY_TYPE = {c["type"]: c for c in CATALOG}

FILTER_KEYS = ("kind", "category", "window", "hour_from", "hour_to")


def get_catalog(S):
    return {"primitives": copy.deepcopy(CATALOG)}


# === SECTION: helpers ===

def _now_dt(S):
    try:
        return datetime.strptime(S["now"], "%Y-%m-%dT%H:%M")
    except Exception:
        return datetime(2026, 10, 19, 9, 0)


def _now_date(S):
    return _now_dt(S).date()


def _parse_date_str(s):
    """Return a date for 'YYYY-MM-DD' or 'YYYY-MM' or None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d
        except Exception:
            continue
    return None


def _item_start_parts(item):
    """Return (date_str, hour) or (None, None)."""
    s = item.get("start")
    if not isinstance(s, str) or len(s) < 13:
        return None, None
    d = s[:10]
    try:
        h = int(s[11:13])
    except Exception:
        return None, None
    return d, h


def _matches(filter_, item):
    if not isinstance(filter_, dict):
        return True
    for k, v in filter_.items():
        if k == "kind":
            if item.get("kind") != v:
                return False
        elif k == "window":
            if item.get("window") != v:
                return False
        elif k == "category":
            if item.get("category") != v:
                return False
        elif k == "hour_from":
            d, h = _item_start_parts(item)
            if h is None:
                return False
            try:
                if not (h >= int(v)):
                    return False
            except Exception:
                return False
        elif k == "hour_to":
            d, h = _item_start_parts(item)
            if h is None:
                return False
            try:
                if not (h < int(v)):
                    return False
            except Exception:
                return False
        else:
            return False
    return True


def _shown_ids(S, spec):
    """Return set of item ids shown by spec."""
    shown = set()
    if not isinstance(spec, dict):
        return shown
    elements = spec.get("elements")
    if not isinstance(elements, dict):
        return shown
    root = spec.get("root")
    if root not in elements:
        return shown

    items = S.get("items", []) or []

    def items_for_element(el):
        t = el.get("type")
        props = el.get("props") or {}
        out = []
        if t == "MonthGrid":
            month = props.get("month")
            md = _parse_date_str(month) if isinstance(month, str) else None
            if md is None:
                return out
            mkey = md.strftime("%Y-%m")
            for it in items:
                d, h = _item_start_parts(it)
                if d and d[:7] == mkey:
                    out.append(it)
        elif t == "WeekColumns":
            start = props.get("start")
            sd = _parse_date_str(start) if isinstance(start, str) else None
            days = props.get("days")
            if sd is None or not isinstance(days, int) or isinstance(days, bool):
                return out
            end = sd + timedelta(days=days)
            for it in items:
                d, h = _item_start_parts(it)
                if not d:
                    continue
                try:
                    dd = datetime.strptime(d, "%Y-%m-%d").date()
                except Exception:
                    continue
                if sd <= dd < end:
                    out.append(it)
        elif t == "DayTimeline":
            dstr = props.get("date")
            dd = _parse_date_str(dstr) if isinstance(dstr, str) else None
            fh = props.get("from_hour")
            th = props.get("to_hour")
            if dd is None:
                return out
            if not isinstance(fh, int) or isinstance(fh, bool):
                fh = 0
            if not isinstance(th, int) or isinstance(th, bool):
                th = 24
            dkey = dd.strftime("%Y-%m-%d")
            for it in items:
                d, h = _item_start_parts(it)
                if d == dkey and h is not None and fh <= h < th:
                    out.append(it)
        elif t == "AgendaList":
            f = props.get("from")
            t2 = props.get("to")
            fd = _parse_date_str(f) if isinstance(f, str) else None
            td = _parse_date_str(t2) if isinstance(t2, str) else None
            if fd is None or td is None:
                return out
            for it in items:
                d, h = _item_start_parts(it)
                if not d:
                    continue
                try:
                    dd = datetime.strptime(d, "%Y-%m-%d").date()
                except Exception:
                    continue
                if fd <= dd <= td:
                    out.append(it)
        elif t == "Bucket":
            flt = props.get("filter")
            if not isinstance(flt, dict):
                flt = {}
            for it in items:
                if _matches(flt, it):
                    out.append(it)
        elif t == "Lane":
            flt = props.get("filter")
            if not isinstance(flt, dict):
                flt = {}
            children = el.get("children") or []
            if children:
                child_items = []
                for cid in children:
                    cel = elements.get(cid)
                    if isinstance(cel, dict):
                        child_items.extend(items_for_element(cel))
                for it in child_items:
                    if _matches(flt, it):
                        out.append(it)
            else:
                for it in items:
                    if _matches(flt, it):
                        out.append(it)
        return out

    visited = set()

    def walk(eid):
        if eid in visited:
            return
        visited.add(eid)
        el = elements.get(eid)
        if not isinstance(el, dict):
            return
        for it in items_for_element(el):
            iid = it.get("id")
            if iid is not None:
                shown.add(iid)
        for cid in (el.get("children") or []):
            walk(cid)

    walk(root)
    return shown


def hidden_counts(S, spec):
    """Return {date or 'unscheduled': count} of items not shown by spec."""
    shown = _shown_ids(S, spec)
    counts = {}
    for it in (S.get("items", []) or []):
        iid = it.get("id")
        if iid in shown:
            continue
        d, h = _item_start_parts(it)
        key = d if d else "unscheduled"
        counts[key] = counts.get(key, 0) + 1
    return counts


# === SECTION: critic ===

def _check_prop(el_id, el_type, prop_name, prop_kind, value, problems):
    if prop_kind == "date":
        if not isinstance(value, str):
            problems.append("element '%s' prop '%s' is not a valid date: %r" % (el_id, prop_name, value))
            return
        ok = False
        for fmt in ("%Y-%m-%d", "%Y-%m"):
            try:
                datetime.strptime(value, fmt)
                ok = True
                break
            except Exception:
                continue
        if not ok:
            problems.append("element '%s' prop '%s' is not a valid date: %r" % (el_id, prop_name, value))
    elif prop_kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append("element '%s' prop '%s' must be an int" % (el_id, prop_name))
            return
        if prop_name in ("from_hour", "to_hour"):
            if not (0 <= value <= 24):
                problems.append("element '%s' prop '%s' must be an int" % (el_id, prop_name))
        elif prop_name == "days":
            if not (1 <= value <= 31):
                problems.append("element '%s' prop '%s' must be an int" % (el_id, prop_name))
    elif prop_kind == "str":
        if not isinstance(value, str):
            problems.append("element '%s' prop '%s' must be a str" % (el_id, prop_name))
    elif prop_kind == "filter":
        if not isinstance(value, dict):
            problems.append("element '%s' prop '%s' must be a filter" % (el_id, prop_name))
            return
        for k in value.keys():
            if k not in FILTER_KEYS:
                problems.append("element '%s' prop '%s' has unknown filter key '%s'" % (el_id, prop_name, k))


def critic(S, spec):
    problems = []
    if not isinstance(spec, dict):
        return {"ok": False, "problems": ["spec is not an object"], "repaired": None}

    elements = spec.get("elements")
    if not isinstance(elements, dict):
        return {"ok": False, "problems": ["spec 'elements' is not an object"], "repaired": None}

    root = spec.get("root")
    if root not in elements:
        problems.append("root '%s' missing from elements" % (root,))

    structural = bool(problems)

    for eid, el in elements.items():
        if not isinstance(el, dict):
            problems.append("element '%s' is not an object" % (eid,))
            structural = True
            continue
        etype = el.get("type")
        if etype not in CATALOG_BY_TYPE:
            problems.append("element '%s' has unknown type '%s' (not in the catalog)" % (eid, etype))
            structural = True
            continue
        cat = CATALOG_BY_TYPE[etype]
        children = el.get("children")
        if children is not None:
            if not isinstance(children, list):
                problems.append("element '%s' children must be a list" % (eid,))
                structural = True
            else:
                if not cat["children"] and children:
                    problems.append("element '%s' (%s) cannot have children" % (eid, etype))
                    structural = True
                for cid in children:
                    if cid not in elements:
                        problems.append("element '%s' child '%s' missing from elements" % (eid, cid))
                        structural = True
        props = el.get("props")
        if props is None:
            props = {}
        if not isinstance(props, dict):
            problems.append("element '%s' props must be an object" % (eid,))
            structural = True
            continue
        for pname, pval in props.items():
            if pname not in cat["props"]:
                problems.append("element '%s' has unknown prop '%s'" % (eid, pname))
                continue
            _check_prop(eid, etype, pname, cat["props"][pname], pval, problems)

    if not structural:
        shown = _shown_ids(S, spec)
        hc = spec.get("hidden_counts", {})
        if not isinstance(hc, dict):
            hc = {}
        for it in (S.get("items", []) or []):
            iid = it.get("id")
            if iid in shown:
                continue
            d, h = _item_start_parts(it)
            key = d if d else "unscheduled"
            cnt = hc.get(key)
            if not (isinstance(cnt, int) and cnt >= 1):
                title = it.get("title", iid)
                problems.append("item '%s' (%s) is hidden: neither shown nor counted" % (title, key))

    ok = not problems
    repaired = None
    if not ok:
        r = repair(S, spec)
        if r is not None:
            rc = critic(S, r)
            if rc["ok"]:
                repaired = r
    return {"ok": ok, "problems": problems, "repaired": repaired}


# === SECTION: repair ===

def repair(S, spec):
    if not isinstance(spec, dict):
        spec = {}
    try:
        out = copy.deepcopy(spec)
    except Exception:
        out = {}
    if not isinstance(out, dict):
        out = {}
    elements = out.get("elements")
    if not isinstance(elements, dict):
        elements = {}
    out["elements"] = elements

    today_d = _now_date(S)
    today_s = today_d.strftime("%Y-%m-%d")

    # drop elements of unknown type
    for eid in list(elements.keys()):
        el = elements.get(eid)
        if not isinstance(el, dict) or el.get("type") not in CATALOG_BY_TYPE:
            del elements[eid]

    # clean children lists
    for eid in list(elements.keys()):
        el = elements[eid]
        if not isinstance(el, dict):
            del elements[eid]
            continue
        cat = CATALOG_BY_TYPE.get(el.get("type"))
        if cat is None:
            del elements[eid]
            continue
        children = el.get("children")
        if not isinstance(children, list):
            children = []
        new_children = []
        for cid in children:
            if cid in elements and cat["children"]:
                new_children.append(cid)
        if cat["children"]:
            el["children"] = new_children
        else:
            el.pop("children", None)

    # fix props
    for eid, el in elements.items():
        cat = CATALOG_BY_TYPE.get(el.get("type"))
        if cat is None:
            continue
        props = el.get("props")
        if not isinstance(props, dict):
            props = {}
        new_props = {}
        for pname, pval in props.items():
            if pname not in cat["props"]:
                continue
            pkind = cat["props"][pname]
            if pkind == "date":
                if not isinstance(pval, str):
                    continue
                ok = False
                for fmt in ("%Y-%m-%d", "%Y-%m"):
                    try:
                        datetime.strptime(pval, fmt)
                        ok = True
                        break
                    except Exception:
                        continue
                if ok:
                    new_props[pname] = pval
                else:
                    if pname == "month":
                        new_props[pname] = today_d.replace(day=1).strftime("%Y-%m-%d")
                    else:
                        new_props[pname] = today_s
            elif pkind == "int":
                if isinstance(pval, int) and not isinstance(pval, bool):
                    if pname in ("from_hour", "to_hour"):
                        if 0 <= pval <= 24:
                            new_props[pname] = pval
                    elif pname == "days":
                        if 1 <= pval <= 31:
                            new_props[pname] = pval
            elif pkind == "str":
                if isinstance(pval, str):
                    new_props[pname] = pval
            elif pkind == "filter":
                if isinstance(pval, dict):
                    new_props[pname] = {k: v for k, v in pval.items() if k in FILTER_KEYS}
                else:
                    new_props[pname] = {}
        # fill defaults for missing required props
        for pname, pkind in cat["props"].items():
            if pname in new_props:
                continue
            if pkind == "date":
                if pname == "month":
                    new_props[pname] = today_d.replace(day=1).strftime("%Y-%m-%d")
                else:
                    new_props[pname] = today_s
            elif pkind == "int":
                if pname == "from_hour":
                    new_props[pname] = 0
                elif pname == "to_hour":
                    new_props[pname] = 24
                elif pname == "days":
                    new_props[pname] = 7
            elif pkind == "str":
                new_props[pname] = ""
            elif pkind == "filter":
                new_props[pname] = {}
        el["props"] = new_props

    # ensure root
    root = out.get("root")
    if root not in elements:
        # find elements nobody lists as a child
        listed = set()
        for el in elements.values():
            for cid in (el.get("children") or []):
                listed.add(cid)
        orphans = [eid for eid in elements.keys() if eid not in listed]
        new_root = "repair_root"
        elements[new_root] = {
            "type": "Lane",
            "props": {"label": "All", "filter": {}},
            "children": orphans,
        }
        out["root"] = new_root
        root = new_root

    # if any item still not shown, add catch-all
    shown = _shown_ids(S, out)
    all_items = S.get("items", []) or []
    missing = [it for it in all_items if it.get("id") not in shown]
    if missing:
        rest_id = "repair_rest"
        elements[rest_id] = {
            "type": "Bucket",
            "props": {"label": "Everything else", "filter": {}},
            "children": [],
        }
        elements[rest_id].pop("children", None)
        new_root_id = "repair_root"
        old_root = out.get("root")
        elements[new_root_id] = {
            "type": "Lane",
            "props": {"label": "All", "filter": {}},
            "children": [old_root, rest_id],
        }
        out["root"] = new_root_id

    return out


# === SECTION: designer ===

def _pick_belief_key(S, belief_key):
    if belief_key:
        return belief_key
    beliefs = S.get("beliefs", {}) or {}
    best_key = None
    best_conf = -1.0
    if isinstance(beliefs, dict):
        for k, b in beliefs.items():
            if not isinstance(b, dict):
                continue
            if b.get("level") != "habit":
                continue
            conf = b.get("confidence", 0)
            try:
                conf = float(conf)
            except Exception:
                conf = 0.0
            if conf > best_conf:
                best_conf = conf
                best_key = k
    return best_key or "none"


def _mk_element(elements, eid, etype, props, children=None):
    el = {"type": etype, "props": props}
    if children is not None:
        el["children"] = children
    elements[eid] = el
    return eid


def _draft_evening(S, today_d, monday_d, saturday_d, hours, label_root, label_lane):
    fh, th = hours
    elements = {}
    _mk_element(elements, "root", "Lane", {"label": label_root, "filter": {}},
                ["timeline", "bucket", "rest"])
    _mk_element(elements, "timeline", "DayTimeline",
                {"date": today_d.strftime("%Y-%m-%d"), "from_hour": fh, "to_hour": th})
    _mk_element(elements, "bucket", "Bucket",
                {"label": label_lane, "filter": {"hour_from": fh, "hour_to": th}})
    _mk_element(elements, "rest", "Bucket", {"label": "Everything else", "filter": {}})
    d0 = {"root": "root", "elements": elements}

    elements1 = {}
    _mk_element(elements1, "root", "Lane", {"label": "This week", "filter": {}},
                ["lane", "rest"])
    _mk_element(elements1, "lane", "Lane",
                {"label": label_lane, "filter": {"hour_from": fh, "hour_to": th}},
                ["week"])
    _mk_element(elements1, "week", "WeekColumns",
                {"start": monday_d.strftime("%Y-%m-%d"), "days": 7})
    _mk_element(elements1, "rest", "Bucket", {"label": "Everything else", "filter": {}})
    d1 = {"root": "root", "elements": elements1}
    return d0, d1


def _draft_no_weekend(S, today_d, monday_d, saturday_d):
    sunday_d = saturday_d + timedelta(days=1)
    elements = {}
    _mk_element(elements, "root", "Lane", {"label": "Workweek", "filter": {}},
                ["week", "weekend", "rest"])
    _mk_element(elements, "week", "WeekColumns",
                {"start": monday_d.strftime("%Y-%m-%d"), "days": 5})
    _mk_element(elements, "weekend", "AgendaList",
                {"from": saturday_d.strftime("%Y-%m-%d"), "to": sunday_d.strftime("%Y-%m-%d")})
    _mk_element(elements, "rest", "Bucket", {"label": "Everything else", "filter": {}})
    d0 = {"root": "root", "elements": elements}

    friday_d = monday_d + timedelta(days=4)
    elements1 = {}
    _mk_element(elements1, "root", "Lane", {"label": "Weekdays first", "filter": {}},
                ["weekdays", "weekend", "rest"])
    _mk_element(elements1, "weekdays", "AgendaList",
                {"from": monday_d.strftime("%Y-%m-%d"), "to": friday_d.strftime("%Y-%m-%d")})
    _mk_element(elements1, "weekend", "AgendaList",
                {"from": saturday_d.strftime("%Y-%m-%d"), "to": sunday_d.strftime("%Y-%m-%d")})
    _mk_element(elements1, "rest", "Bucket", {"label": "Everything else", "filter": {}})
    d1 = {"root": "root", "elements": elements1}
    return d0, d1


def _draft_generic(S, today_d, monday_d, saturday_d):
    end_d = today_d + timedelta(days=6)
    elements = {}
    _mk_element(elements, "root", "Lane", {"label": "Agenda", "filter": {}},
                ["agenda", "rest"])
    _mk_element(elements, "agenda", "AgendaList",
                {"from": today_d.strftime("%Y-%m-%d"), "to": end_d.strftime("%Y-%m-%d")})
    _mk_element(elements, "rest", "Bucket", {"label": "Everything else", "filter": {}})
    d0 = {"root": "root", "elements": elements}

    elements1 = {}
    _mk_element(elements1, "root", "Lane", {"label": "Month", "filter": {}},
                ["month", "rest"])
    _mk_element(elements1, "month", "MonthGrid",
                {"month": today_d.replace(day=1).strftime("%Y-%m-%d")})
    _mk_element(elements1, "rest", "Bucket", {"label": "Everything else", "filter": {}})
    d1 = {"root": "root", "elements": elements1}
    return d0, d1


def design(S, belief_key):
    key = _pick_belief_key(S, belief_key)
    today_d = _now_date(S)
    monday_d = today_d - timedelta(days=today_d.weekday())
    saturday_d = monday_d + timedelta(days=5)

    if key == "evening_work":
        d0, d1 = _draft_evening(S, today_d, monday_d, saturday_d, (18, 24),
                                "Your evenings", "Evening tasks")
    elif key == "morning_work":
        d0, d1 = _draft_evening(S, today_d, monday_d, saturday_d, (6, 12),
                                "Your mornings", "Morning lane")
    elif key == "no_weekend":
        d0, d1 = _draft_no_weekend(S, today_d, monday_d, saturday_d)
    else:
        d0, d1 = _draft_generic(S, today_d, monday_d, saturday_d)

    # D2 = D0 without "rest"
    d2 = copy.deepcopy(d0)
    els = d2.get("elements", {})
    els.pop("rest", None)
    for el in els.values():
        if isinstance(el, dict) and isinstance(el.get("children"), list):
            el["children"] = [c for c in el["children"] if c != "rest"]

    drafts = [d0, d1, d2]

    critique = []
    repairs = 0
    chosen = 0
    for i, d in enumerate(drafts):
        c = critic(S, d)
        critique.append({"ok": c["ok"], "problems": c["problems"]})
        if not c["ok"] and c["repaired"] is not None:
            repairs += 1
    for i, c in enumerate(critique):
        if c["ok"]:
            chosen = i
            break
    else:
        chosen = 0

    return {"drafts": drafts, "critique": critique, "repairs": repairs,
            "chosen": chosen, "proposal": None}


def best_spec(S, belief_key):
    r = design(S, belief_key)
    drafts = r["drafts"]
    chosen = r["chosen"]
    if not drafts:
        return {"root": "root", "elements": {}}
    if chosen < 0 or chosen >= len(drafts):
        chosen = 0
    return drafts[chosen]
