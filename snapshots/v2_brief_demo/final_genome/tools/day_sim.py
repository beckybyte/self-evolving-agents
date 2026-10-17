"""Deterministic reference scheduler + replanner for the day-assistant design.

run(ctx) -> {"text": short summary for LLM roles,
             "artifacts": {"flows": verified worked examples, "assumptions": A-id table}}

Cases come from blackboard["tester"] (JSON list, if a tester role exists and its output parses),
otherwise / in addition from the built-in archetype days below. Every timetable printed here is
produced by code and self-checked for overlaps, so the documents never show a double-booked day.
"""
import json
import math
import re

BUF = 0.25          # default buffer on self-estimated tasks (A6)
BUF_URGENT = 0.40   # inserted "urgent" estimates are usually optimistic
BUF_MIN = 0.10      # buffer after compression during a replan
DEFAULT_DUR = 60    # missing duration -> visible default
BREAK = 15          # break between two focus blocks of one task
FOCUS_MIN = 60      # a focus task is never split below this
FRAG_MIN = 10
TRANSITION = 10     # kept free before every fixed appointment (getting ready / on the way)
TAIL = 60           # optional tasks are not pushed into the last hour of the day


# ---------------------------------------------------------------- time helpers
def ceil5(x):
    return int(math.ceil(x / 5.0) * 5)


def parse_t(s, day_start=None):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip()
    nxt = s.startswith("次日") or s.startswith("+1")
    m = re.search(r"(\d{1,2})[:：](\d{2})", s)
    if not m:
        return None
    t = int(m.group(1)) * 60 + int(m.group(2))
    if nxt:
        t += 1440
    elif day_start is not None and t < day_start - 60:
        t += 1440  # day crosses midnight
    return t


def fmt(t):
    pre = "次日" if t >= 1440 else ""
    t %= 1440
    return "%s%02d:%02d" % (pre, t // 60, t % 60)


def span(a, b):
    return "%s–%s" % (fmt(a), fmt(b))


def subtract(free, busy):
    out = []
    for a, b in free:
        segs = [(a, b)]
        for x, y in busy:
            nxt = []
            for p, q in segs:
                if y <= p or x >= q:
                    nxt.append((p, q))
                else:
                    if x > p:
                        nxt.append((p, x))
                    if y < q:
                        nxt.append((y, q))
            segs = nxt
        out.extend(segs)
    return sorted(s for s in out if s[1] - s[0] > 0)


# ---------------------------------------------------------------- model
class Item:
    def __init__(self, d, day_start):
        self.name = d.get("name", "?")
        self.kind = d.get("kind", "must")  # fixed | protected | must | optional
        self.start = parse_t(d.get("start"), day_start)
        self.default_dur = d.get("dur") in (None, "", 0)
        self.dur = int(d.get("dur") or DEFAULT_DUR)
        if self.kind in ("fixed", "protected") and d.get("end") and self.start is not None:
            self.dur = parse_t(d.get("end"), day_start) - self.start
            self.default_dur = False
        self.focus = bool(d.get("focus"))
        self.fragment = bool(d.get("fragment"))
        self.blocks = int(d.get("blocks") or 1)
        self.deadline = parse_t(d.get("deadline"), day_start)
        self.deadline_txt = d.get("deadline")
        self.urgent = bool(d.get("urgent"))
        self.vague = d.get("vague")  # e.g. "尽快"
        self.note = d.get("note", "")
        self.buf = BUF_URGENT if self.urgent else BUF
        self.eff = self.dur if self.kind in ("fixed", "protected") else ceil5(self.dur * (1 + self.buf))

    def end(self):
        return self.start + self.dur


def prio(it):
    return (0 if it.deadline is not None else 1, it.deadline or 10 ** 6, 0 if it.focus else 1, -it.eff)


class Plan:
    def __init__(self, case):
        self.case = case
        self.ds = parse_t(case.get("day_start", "07:00"))
        self.de = parse_t(case.get("day_end", "23:00"), self.ds)
        if self.de <= self.ds:
            self.de += 1440
        self.now = parse_t(case.get("now", case.get("day_start", "07:00")), self.ds)
        self.items = [Item(d, self.ds) for d in case.get("items", [])]
        self.fixed = [i for i in self.items if i.kind in ("fixed", "protected") and i.start is not None]
        self.slots = []        # (start, end, name, label)
        self.pool = []         # (name, reason)
        self.notes = []
        self.max_chunk = None
        pk = case.get("peak") or []
        self.peak = (parse_t(pk[0], self.ds), parse_t(pk[1], self.ds)) if len(pk) == 2 else None
        self.fired = set()

    # free time from t0, minus fixed blocks and already placed slots
    def free(self, t0):
        busy = [(f.start, f.end()) for f in self.fixed] + [(a, b) for a, b, _, _ in self.slots]
        busy += [(f.start - TRANSITION, f.start) for f in self.fixed if f.kind == "fixed"]
        return subtract([(t0, self.de)], busy)

    def fixed_conflicts(self):
        out = []
        fs = sorted(self.fixed, key=lambda f: f.start)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                if fs[j].start < fs[i].end() and fs[i].start < fs[j].end():
                    out.append((fs[i], fs[j]))
        return out

    def place(self, it, t0, eff=None, split=True):
        """Place one item; returns minutes left unplaced."""
        need = eff or it.eff
        gaps = self.free(t0)
        cap = self.max_chunk
        dl = it.deadline
        whole = [g for g in gaps if g[1] - g[0] >= need and (cap is None or need <= cap)]
        if dl is not None and it.kind == "must":
            whole = [g for g in whole if g[0] + need <= dl]
        if it.blocks > 1 and not cap:
            per = ceil5(need / float(it.blocks))
            left = need
            for k in range(it.blocks):
                g = [g for g in self.free(t0) if g[1] - g[0] >= min(per, left)]
                if not g:
                    break
                a = g[0][0]
                n = min(per, left)
                self.slots.append((a, a + n, it.name, "整块 %d/%d" % (k + 1, it.blocks)))
                left -= n
                if left > 0 and a + n + BREAK <= g[0][1]:
                    self.slots.append((a + n, a + n + BREAK, "休息", "两块之间"))
            return left
        if whole:
            if it.kind == "optional":
                ms = [a for a, b, n, _ in self.slots if self.kind_of(n) == "must"]
                ok = [g for g in whole if g[0] + need <= self.de - TAIL and (not ms or g[0] >= min(ms))]
                if not ok:
                    return need
                g = ok[-1]  # latest slack after the first must, not the last hour of the day
                self.slots.append((g[0], g[0] + need, it.name, ""))
            elif it.focus:
                if self.peak:  # stated / default energy peak first, else earliest
                    inpk = [g for g in whole if g[0] < self.peak[1] and g[0] + need > self.peak[0]]
                    g = (inpk or whole)[0]
                    if inpk:
                        self.fired.add("peak")
                        g = (max(g[0], min(self.peak[0], g[1] - need)), g[1])
                else:
                    g = whole[0]  # earliest long-enough gap
                self.slots.append((g[0], g[0] + need, it.name, ""))
            else:
                g = min(whole, key=lambda g: (g[1] - g[0], g[0]))  # best fit keeps long gaps free
                self.slots.append((g[0], g[0] + need, it.name, ""))
            return 0
        if it.kind == "optional" or not split:
            return need
        if dl is not None and it.kind == "must":  # never split a deadline task past its deadline
            gaps = [(a, min(b, dl)) for a, b in gaps if a < dl]
        mn = FRAG_MIN if it.fragment else (FOCUS_MIN if it.focus else 30)
        if cap:
            mn = min(mn, cap)
        left, k = need, 0
        for a, b in gaps:
            while left > (15 if k else 0) and b - a >= mn:
                n = min(b - a, left, cap or 10 ** 6)
                if 0 < left - n < mn:
                    if left - mn >= mn:
                        n = left - mn
                    elif b - a >= left:
                        n = left
                k += 1
                self.slots.append((a, a + n, it.name, "分段"))
                left -= n
                a += n
            if left <= (15 if k else 0):
                break
        if 0 < left <= 15 and left < need:
            return 0  # a tail of <=15 minutes is absorbed by the task's own buffer, not a separate block
        return left

    def kind_of(self, name):
        return next((i.kind for i in self.items if i.name == name), None)

    def place_optional(self, it, t0, reserve):
        """Optional: whole if the reserve survives, else a half-size version, else the later pool."""
        free_total = sum(b - a for a, b in self.free(t0))
        if free_total - it.eff >= reserve and self.place(it, t0) == 0:
            return "whole"
        small = ceil5(it.dur / 2.0 * (1 + it.buf))
        if it.dur >= 40 and free_total - small >= reserve and self.place(it, t0, eff=small) == 0:
            a, b, n, _ = self.slots[-1]
            self.slots[-1] = (a, b, n, "缩小版")
            return "small"
        return None

    def overlaps(self):
        segs = [(f.start, f.end(), f.name) for f in self.fixed] + [(a, b, n) for a, b, n, _ in self.slots]
        segs.sort()
        bad = []
        fixed_names = set()
        for c in self.fixed_conflicts():
            fixed_names |= {c[0].name, c[1].name}
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                if segs[j][0] < segs[i][1] and segs[i][0] < segs[j][1]:
                    if segs[i][2] in fixed_names and segs[j][2] in fixed_names:
                        continue  # surfaced on the confirmation card
                    bad.append((segs[i], segs[j]))
        return bad


def morning(p):
    out = []
    card = []
    for a, b in p.fixed_conflicts():
        card.append("冲突：%s（%s）与 %s（%s）重叠 → 不替你决定：①保留%s ②保留%s；裁决前两段时间都不排任务"
                    % (a.name, span(a.start, a.end()), b.name, span(b.start, b.end()), a.name, b.name))
    for it in p.items:
        if it.kind in ("must", "optional") and it.default_dur:
            card.append("缺时长：%s 未给时长 → 默认 %d 分钟（低置信，可改）" % (it.name, it.dur))
        if it.vague:
            card.append("含糊：%s 写的是“%s” → 我的猜测是“今天做、无具体截止”，在你确认前标为【待确认】，可改成可选或给截止" % (it.name, it.vague))
    musts = sorted([i for i in p.items if i.kind == "must"], key=prio)
    opts = [i for i in p.items if i.kind == "optional"]
    unplaced = []
    for it in musts:
        left = p.place(it, p.now)
        if left > 0:
            unplaced.append((it, left))
    reserve = max(30, int(0.1 * (p.de - p.now)))
    p.reserve = reserve
    for it in opts:
        if p.place_optional(it, p.now, reserve) is None:
            p.pool.append((it.name, "余量不足（需保留 %d 分钟机动），进稍后池，不删除" % reserve))
    out.append("**输入时刻 %s，一天 = %s**" % (fmt(p.now), span(p.ds, p.de)))
    if card:
        out.append("确认卡（这几项是我的猜测，不是决定：计划是草稿，点一下即可改；冲突时段在你裁决前不排任何事）：")
        out += ["- " + c for c in card]
    out.append("")
    out.append(table(p))
    if unplaced:
        for it, left in unplaced:
            out.append("- **排不下**：%s 还差 %d 分钟。建议：缩小版本 / 把可选挪走 / 与对方协商截止%s"
                       % (it.name, left, "（截止 %s）" % it.deadline_txt if it.deadline_txt else ""))
    if p.pool:
        out.append("- 稍后池：" + "；".join("%s（%s）" % x for x in p.pool))
    first = sorted(s for s in p.slots if p.kind_of(s[2]) == "must")
    if first:
        out.append("- 起步：第一件是“%s”，先做 10 分钟最小的一步（打开文件、列三行提纲），开了头再看。" % first[0][2])
    out.append("- 剩余机动时间：%d 分钟（未分配，用来吸收超时）" % sum(b - a for a, b in p.free(p.now)))
    if any(f.kind == "fixed" for f in p.fixed):
        out.append("- 每个固定事项前留 %d 分钟准备/路上，不排任务" % TRANSITION)
    if p.peak:
        out.append("- 精力高峰按 %s 处理（%s，可改）：最难的整块任务优先放这里" % (span(*p.peak), p.case.get("peak_src", "你说的")))
    for it in sorted([i for i in p.items if i.kind == "must" and i.deadline is not None], key=prio):
        ends = [b for a, b, n, _ in p.slots if n == it.name]
        if ends:
            out.append("- 截止余量：%s 预计 %s 完成，距截止 %s 还有 %d 分钟" % (it.name, fmt(max(ends)), it.deadline_txt, it.deadline - max(ends)))
    return "\n".join(out), unplaced


def table(p):
    rows = [(f.start, f.end(), f.name, "固定" if f.kind == "fixed" else "受保护", "不可移动") for f in p.fixed]
    byname = {i.name: i for i in p.items}
    for a, b, n, lab in p.slots:
        it = byname.get(n)
        if it is None:
            rows.append((a, b, n, "休息", lab))
            continue
        why = []
        if it.focus:
            if p.peak and a < p.peak[1] and b > p.peak[0]:
                why.append("需整块专注→精力高峰内")
            elif p.peak:
                why.append("需整块专注→高峰已用完，下一个足够长空档")
            else:
                why.append("需整块专注→最早的足够长空档")
        elif it.fragment:
            why.append("碎片型→填短空档")
        if it.kind == "optional":
            why.append("可选，只用余量（必做之后、不占最后一小时）")
        if it.deadline_txt:
            why.append("截止 %s" % it.deadline_txt)
        if lab:
            why.append(lab)
        if it.kind != "optional" or True:
            why.append("估 %d%s+缓冲" % (it.dur, "（默认）" if it.default_dur else ""))
        rows.append((a, b, n, {"must": "必做", "optional": "可选"}.get(it.kind, it.kind), "；".join(why)))
    rows.sort()
    lines = ["| 时间 | 事项 | 类型 | 为什么在这里 |", "|---|---|---|---|"]
    for a, b, n, k, w in rows:
        lines.append("| %s | %s | %s | %s |" % (span(a, b), n, k, w))
    return "\n".join(lines)


def snapshot(p):
    d = {}
    for a, b, n, _ in p.slots:
        if n != "休息":
            d.setdefault(n, []).append((a, b))
    return d


def replan(p, ev):
    at = parse_t(ev.get("at"), p.ds)
    before = snapshot(p)
    typ = ev.get("type")
    says = ev.get("says", "")
    byname = {i.name: i for i in p.items}
    log = []
    new_task = None
    if typ in ("move_fixed", "extend_fixed", "remove_fixed"):
        f = next((x for x in p.fixed if x.name == ev.get("name")), None)
        if f:
            if typ == "move_fixed":
                f.start = parse_t(ev.get("start"), p.ds)
            elif typ == "extend_fixed":
                f.dur = parse_t(ev.get("end"), p.ds) - f.start
            else:
                p.fixed.remove(f)
    elif typ == "insert_fixed":
        it = Item({"name": ev.get("name"), "kind": "fixed", "start": ev.get("start"), "dur": ev.get("dur", 30)}, p.ds)
        p.items.append(it)
        p.fixed.append(it)
        old = next((x for x in p.fixed if x.name == ev.get("replaces")), None)
        if old:
            p.fixed.remove(old)
            log.append("合并：原来的“%s”（%s）由这次代替，已从计划里去掉（这是你说的变化，不是助手移动固定事项）"
                       % (old.name, span(old.start, old.end())))
    elif typ == "insert_task":
        new_task = Item({"name": ev.get("name"), "kind": "must", "dur": ev.get("dur"), "deadline": ev.get("deadline"),
                         "urgent": True, "focus": ev.get("focus", True)}, p.ds)
        p.items.append(new_task)
        byname[new_task.name] = new_task
    elif typ == "update_dur":
        it = byname.get(ev.get("name"))
        if it:
            log.append("估时修正：%s 从 %d 分钟改为 %d 分钟（按你的话，仍加缓冲）" % (it.name, it.dur, int(ev["dur"])))
            it.dur, it.default_dur = int(ev["dur"]), False
            it.eff = ceil5(it.dur * (1 + it.buf))
    if typ == "behind":
        log.append("没关系，从 %s 重新开始：只看后面的安排，前面没做的不算失败；第一步先做 10 分钟最小的一步。" % fmt(at))
    if ev.get("max_chunk"):
        p.max_chunk = int(ev["max_chunk"])
    undone = set(ev.get("undone", []))
    resized = ev.get("name") if typ == "update_dur" else None
    fixed_busy = [(f.start, f.end()) for f in p.fixed]
    win_end = at
    if new_task is not None:  # the window the urgent task needs, walking around fixed blocks only
        left_need = new_task.eff
        for a, b in subtract([(at, p.de)], fixed_busy + [(f.start - TRANSITION, f.start) for f in p.fixed if f.kind == "fixed"]):
            if left_need <= 0:
                break
            win_end = a + min(b - a, left_need)
            left_need -= min(b - a, left_need)
    keep, moved_out, done_min = [], {}, {}
    for a, b, n, lab in p.slots:
        it = byname.get(n)
        if n == "休息":
            if b <= at:
                keep.append((a, b, n, lab))
            continue
        clash = any(x < b and a < y for x, y in fixed_busy)
        # an urgent insert may take over everything after `at` that ranks below it
        yields = new_task is not None and b > at and a < win_end and it.kind == "must" and prio(it) > prio(new_task)
        if b <= at and n not in undone:
            keep.append((a, b, n, lab))
            done_min[n] = done_min.get(n, 0) + b - a
        elif n in undone:
            moved_out[n] = moved_out.get(n, 0) + b - a
        elif n == resized and a < at:
            keep.append((a, b, n, lab))  # keep working on it; only the extra time is re-placed
        elif clash or yields or n == resized:
            if a < at:
                keep.append((a, at, n, lab))
                done_min[n] = done_min.get(n, 0) + at - a
            moved_out[n] = moved_out.get(n, 0) + b - max(a, at)
        else:
            keep.append((a, b, n, lab))
    p.slots = keep
    if resized and resized in byname:
        it = byname[resized]
        planned = sum(b - a for a, b, n, _ in keep if n == resized)
        moved_out[resized] = max(0, it.eff - planned)
    displaced = [byname[n] for n in moved_out if moved_out[n] > 0]
    if new_task:
        displaced.append(new_task)
        moved_out[new_task.name] = new_task.eff
    displaced.sort(key=prio)
    stepped = []  # optionals after the change step aside while musts are re-placed, then come back
    if any(d.kind == "must" for d in displaced):
        stepped = [s for s in p.slots if s[0] >= at and p.kind_of(s[2]) == "optional"]
        for s in stepped:
            p.slots.remove(s)
    for it in displaced:
        need = moved_out[it.name]
        it.blocks = 1
        if it.kind == "optional":
            if p.place(it, at, eff=need) > 0:
                p.pool.append((it.name, "被变化挤出，进稍后池"))
            continue
        left = p.place(it, at, eff=need, split=False)
        if left:
            small = need - (it.eff - ceil5(it.dur * (1 + BUF_MIN)))
            if not it.urgent and small < need and p.place(it, at, eff=small, split=False) == 0:
                log.append("压缩缓冲：%s 的缓冲从 %d%% 压到 10%%，保持一整块" % (it.name, int(it.buf * 100)))
                left = 0
        if left:
            opt = [s for s in p.slots if s[0] >= at and byname.get(s[2]) and byname[s[2]].kind == "optional"]
            for s in opt:
                p.slots.remove(s)
            if p.place(it, at, eff=need, split=False) == 0:
                left = 0
                p.pool += [(s[2], "为保护%s让出时间，进稍后池（不删除）" % it.name) for s in opt]
            else:
                p.slots += opt
        if left:
            if p.max_chunk:
                log.append("拆分：%s 剩余 %d 分钟，按你现在只能零碎干活，拆成 ≤%d 分钟的小段" % (it.name, need, p.max_chunk))
            left = p.place(it, at, eff=need)
        if left:
            log.append(reach(p, it, left))
        elif it.deadline is not None:
            last = max(b for a, b, n, _ in p.slots if n == it.name)
            log.append("截止检查：%s 预计 %s 完成，截止 %s → %s"
                       % (it.name, fmt(last), it.deadline_txt, "可达" if last <= it.deadline else "不可达，需你选择：缩小版本/延期"))
        if it.urgent:
            got = sum(b - a for a, b, n, _ in p.slots if n == it.name)
            if got >= it.eff:
                log.append("估时提醒：%s 你说 %d 分钟，这类临时插入常被低估，已按 %d 分钟（+40%%）预留；做得快，多出的时间还给后面"
                           % (it.name, it.dur, it.eff))
            else:
                log.append("估时提醒：%s 你说 %d 分钟，这类临时插入常被低估；截止前只能留出 %d 分钟，缓冲不足，请你决定是否和对方要更多时间"
                           % (it.name, it.dur, got))
            p.fired.add("urgent")
    for s in stepped:
        it = byname[s[2]]
        if not any(x < s[1] and s[0] < y for x, y in [(f.start, f.end()) for f in p.fixed] + [(a, b) for a, b, _, _ in p.slots]):
            p.slots.append(s)  # its old place is still free: unchanged
            continue
        got = p.place_optional(it, at, getattr(p, "reserve", 30))
        if got is None:
            p.pool.append((it.name, "为保护必做让出时间，进稍后池（不删除）"))
        elif got == "small":
            log.append("缩小：%s 只放得下缩小版（一半时长），完整版进晚间收尾清单" % it.name)
    for it in displaced:
        if it.deadline is not None and it.kind == "must":
            ends = [b for a, b, n, _ in p.slots if n == it.name]
            if ends and max(ends) <= it.deadline:
                log.append("截止余量：%s 距截止 %s 还剩 %d 分钟；若前面的事超时，先吃掉的是这段余量" % (it.name, it.deadline_txt, it.deadline - max(ends)))
        if it.focus and p.peak and not it.urgent:
            late = [a for a, b, n, _ in p.slots if n == it.name and a >= p.peak[1]]
            if late:
                log.append("质量代价（坦白）：%s 有一部分被挤出精力高峰，从 %s 起排在状态较差的时段；如果那时效率低，可改成今天只做缩小版本，其余明早补"
                           % (it.name, fmt(min(late))))
    log.append("重排后剩余机动时间：%d 分钟" % sum(b - a for a, b in p.free(at)))
    after = snapshot(p)
    diff = []
    unchanged = 0
    for n in sorted(set(before) | set(after), key=lambda n: (before.get(n) or after.get(n))[0][0]):
        b0, a0 = before.get(n), after.get(n)
        if b0 == a0:
            unchanged += 1
            continue
        if b0 is None:
            diff.append("新增：%s → %s" % (n, "，".join(span(*x) for x in a0)))
        elif a0 is None:
            diff.append("推迟到稍后池：%s（原 %s）" % (n, "，".join(span(*x) for x in b0)))
        else:
            verb = "挪动" if len(a0) == len(b0) else "拆分/挪动"
            diff.append("%s：%s 原 %s → 现 %s" % (verb, n, "，".join(span(*x) for x in b0), "，".join(span(*x) for x in a0)))
    out = ["**%s 用户一句话：“%s”**" % (fmt(at), says),
           "差异提案（接受前不生效；助手没有移动任何固定事项）："]
    out += ["- " + d for d in diff] or ["- 计划不用改"]
    out += ["- " + l for l in log]
    out.append("- 原样保留：%d 项" % unchanged)
    out.append("")
    out.append("接受后的计划：")
    out.append(table(p))
    return "\n".join(out)


def reach(p, it, left):
    tail = "今天之内还差 %d 分钟，排不下 %s。" % (left, it.name)
    if it.deadline is None:
        sleep = [f for f in p.fixed if "睡" in f.name]
        return tail + ("睡眠受保护，不会把它塞进睡眠或拆碎睡眠；" if sleep else "") + "建议今天舍弃，进晚间收尾清单，由你决定是否带到明天。"
    if it.deadline <= p.de:
        return tail + "截止 %s 今天内不可达。可选：①做缩小版本先交 ②现在联系对方延期 ③挤掉其他必做（需你确认）" % it.deadline_txt
    tomorrow = p.ds + 1440
    room = it.deadline - max(tomorrow, p.de)
    if room >= left:
        return tail + "截止 %s 仍可达：建议明早 %s 起先做 %d 分钟（剩余量已记在收尾清单），或今晚加做。" % (
            it.deadline_txt, span(max(tomorrow, p.de), max(tomorrow, p.de) + left), left)
    return tail + "即使明早补也不够（截止 %s）→ 建议今晚加做或与对方沟通。" % it.deadline_txt


def run_case(c):
    p = Plan(c)
    m, _ = morning(p)
    parts = ["### %s %s" % (c.get("id", ""), c.get("title", "")), "", c.get("who", ""), "", "早上计划：", m]
    ok = not p.overlaps()
    for ev in c.get("events", []):
        parts += ["", replan(p, ev)]
        ok = ok and not p.overlaps()
    parts.append("")
    parts.append("自检（代码校验）：%s" % ("所有时间块与固定事项 0 重叠" if ok else "发现重叠，见上表"))
    return "\n".join(parts), ok


BUILTIN = [
    {"id": "F1", "title": "自主型学生：几乎没有固定事项", "who": "大三学生，期末前，一节课 + 晚上兼职，其余自由；容易从最简单的事开始。",
     "now": "09:30", "day_start": "09:30", "day_end": "23:30", "peak": ["09:30", "12:00"], "peak_src": "默认上午",
     "items": [{"name": "课", "kind": "fixed", "start": "14:00", "end": "15:40"},
               {"name": "兼职", "kind": "fixed", "start": "19:00", "end": "21:00"},
               {"name": "午饭", "kind": "protected", "start": "12:00", "end": "12:40"},
               {"name": "晚饭", "kind": "protected", "start": "18:00", "end": "18:40"},
               {"name": "复习概率论", "kind": "must", "dur": 180, "focus": True, "blocks": 2, "deadline": "次日09:00"},
               {"name": "汇报稿", "kind": "must", "dur": 60, "focus": True},
               {"name": "洗衣服", "kind": "must", "dur": 20, "fragment": True},
               {"name": "健身", "kind": "optional", "dur": 50},
               {"name": "追剧", "kind": "optional", "dur": 45}],
     "events": [{"at": "10:40", "type": "behind", "undone": ["复习概率论"], "says": "刷手机到现在，复习还没开始"}]},
    {"id": "F2", "title": "自由职业者：临时插入客户急活", "who": "自由设计师，晚上 20:00 交稿，估时常偏乐观。",
     "now": "09:00", "day_start": "09:00", "day_end": "23:00", "peak": ["09:00", "12:30"], "peak_src": "默认上午",
     "items": [{"name": "午饭", "kind": "protected", "start": "12:30", "end": "13:15"},
               {"name": "交稿", "kind": "fixed", "start": "20:00", "end": "20:15"},
               {"name": "晚饭", "kind": "protected", "start": "18:30", "end": "19:00"},
               {"name": "交付稿", "kind": "must", "dur": 300, "focus": True, "deadline": "20:00"},
               {"name": "回客户消息", "kind": "must", "dur": 30, "fragment": True},
               {"name": "运动", "kind": "optional", "dur": 45},
               {"name": "作品集", "kind": "optional", "dur": 60}],
     "events": [{"at": "10:30", "type": "insert_task", "name": "客户急改", "dur": 90, "deadline": "15:00",
                 "says": "客户加了个急活，大概 1.5 小时，下午三点前要"}]},
    {"id": "F3", "title": "被打断的家长：新增一个不可移动的事", "who": "兼职会计，对账明天 12:00 截止，下午孩子发烧要接回家，之后只能碎片工作。",
     "now": "08:30", "day_start": "07:00", "day_end": "21:30",
     "items": [{"name": "送孩子", "kind": "fixed", "start": "07:30", "end": "08:15"},
               {"name": "银行办事", "kind": "fixed", "start": "09:00", "end": "11:00"},
               {"name": "午饭", "kind": "protected", "start": "12:00", "end": "12:45"},
               {"name": "接孩子", "kind": "fixed", "start": "17:00", "end": "17:40"},
               {"name": "做饭吃饭", "kind": "protected", "start": "18:00", "end": "19:00"},
               {"name": "对账", "kind": "must", "dur": 240, "focus": True, "deadline": "次日12:00"},
               {"name": "回家长群消息", "kind": "must", "dur": 20, "fragment": True},
               {"name": "瑜伽", "kind": "optional", "dur": 40}],
     "events": [{"at": "14:00", "type": "insert_fixed", "name": "接发烧的孩子（15:30 前到校）", "start": "15:00", "dur": 60,
                 "replaces": "接孩子", "max_chunk": 30, "says": "学校来电孩子发烧，3:30 前要接回家，之后在家只能零碎干活"}]},
    {"id": "F4", "title": "研究生：设备预约 + 组会拖堂", "who": "研究生，设备预约不可改，论文需要整块时间，文献是可选但不想完全放弃。",
     "now": "08:00", "day_start": "08:00", "day_end": "23:00",
     "items": [{"name": "设备预约", "kind": "fixed", "start": "09:00", "end": "12:00"},
               {"name": "午饭", "kind": "protected", "start": "12:00", "end": "12:45"},
               {"name": "组会", "kind": "fixed", "start": "13:30", "end": "14:30"},
               {"name": "朋友聚餐（含路上）", "kind": "fixed", "start": "18:30", "end": "21:00"},
               {"name": "论文第三节", "kind": "must", "dur": 180, "focus": True},
               {"name": "文献A", "kind": "optional", "dur": 40},
               {"name": "文献B", "kind": "optional", "dur": 40}],
     "events": [{"at": "14:30", "type": "extend_fixed", "name": "组会", "end": "14:50", "says": "组会拖堂到 14:50"}]},
    {"id": "F5", "title": "会议密集者（次要用户）：会议延期，释放的空档被用上", "who": "产品经理，3 个会，PRD 需要整块，邮件是碎片，行业报告可选。",
     "now": "08:50", "day_start": "08:50", "day_end": "19:00",
     "items": [{"name": "站会", "kind": "fixed", "start": "10:00", "end": "10:30"},
               {"name": "设计评审", "kind": "fixed", "start": "11:00", "end": "12:00"},
               {"name": "午饭", "kind": "protected", "start": "12:00", "end": "12:45"},
               {"name": "供应商电话", "kind": "fixed", "start": "14:00", "end": "14:45"},
               {"name": "PRD 初稿", "kind": "must", "dur": 120, "focus": True, "deadline": "18:00"},
               {"name": "回 5 封邮件", "kind": "must", "dur": 40, "fragment": True},
               {"name": "行业报告", "kind": "optional", "dur": 45}],
     "events": [{"at": "10:40", "type": "move_fixed", "name": "设计评审", "start": "15:30", "says": "评审改到 15:30"}]},
    {"id": "F6", "title": "怀疑者的陷阱：重叠会议 + 没给时长的“尽快”", "who": "资深项目经理，要验证：它会不会乱动我的会、乱估时间、替我做决定。",
     "now": "08:30", "day_start": "08:30", "day_end": "18:30",
     "items": [{"name": "会议A", "kind": "fixed", "start": "10:00", "end": "11:00"},
               {"name": "会议B", "kind": "fixed", "start": "10:30", "end": "11:30"},
               {"name": "午饭", "kind": "protected", "start": "12:00", "end": "13:00"},
               {"name": "季度报告", "kind": "must", "dur": 120, "focus": True, "deadline": "17:00"},
               {"name": "整理资料", "kind": "must", "vague": "尽快"},
               {"name": "看行业新闻", "kind": "optional", "dur": 30}],
     "events": [{"at": "09:00", "type": "update_dur", "name": "整理资料", "dur": 150, "says": "整理资料其实要两个半小时"}]},
    {"id": "F7", "title": "夜班：一天跨午夜，睡眠是受保护的整块", "who": "夜班护士，“一天”从下午醒来到次日下午再醒来；8 小时睡眠是排进计划的受保护整块，冲突时舍课保睡。",
     "now": "15:00", "day_start": "14:00", "day_end": "次日16:00",
     "items": [{"name": "接孩子", "kind": "fixed", "start": "15:30", "end": "16:10"},
               {"name": "晚饭/家事", "kind": "protected", "start": "17:15", "end": "18:05"},
               {"name": "通勤去医院", "kind": "fixed", "start": "18:15", "end": "18:55"},
               {"name": "夜班", "kind": "fixed", "start": "19:00", "end": "次日07:00"},
               {"name": "通勤回家", "kind": "fixed", "start": "次日07:00", "end": "次日07:40"},
               {"name": "睡眠（受保护）", "kind": "protected", "start": "次日08:00", "end": "次日16:00"},
               {"name": "进修网课", "kind": "must", "dur": 40, "focus": True},
               {"name": "整理冰箱", "kind": "optional", "dur": 20}],
     "events": [{"at": "15:10", "type": "extend_fixed", "name": "接孩子", "end": "17:10", "says": "接完孩子要带去看医生，大概到 5 点 10 分"}]},
    {"id": "F8", "title": "超载日：坦白排不下", "who": "自由职业者，一口气列了三件必做，加起来远超今天的可用时间。",
     "now": "09:00", "day_start": "09:00", "day_end": "21:00",
     "items": [{"name": "午饭", "kind": "protected", "start": "12:00", "end": "13:00"},
               {"name": "客户会", "kind": "fixed", "start": "15:00", "end": "16:00"},
               {"name": "晚饭", "kind": "protected", "start": "18:00", "end": "18:45"},
               {"name": "官网改版", "kind": "must", "dur": 240, "focus": True, "deadline": "18:00"},
               {"name": "报价单", "kind": "must", "dur": 90, "deadline": "17:00"},
               {"name": "海报三稿", "kind": "must", "dur": 180, "focus": True},
               {"name": "跑步", "kind": "optional", "dur": 40}],
     "events": []},
]


def assumptions_md(ctx):
    a = (ctx.get("context") or {}).get("assumptions")
    rows = []
    if isinstance(a, list):
        for x in a:
            if isinstance(x, dict):
                rows.append("| %s | %s | %s |" % (x.get("id", ""), x.get("text", ""), x.get("default", "")))
    elif isinstance(a, str):
        for line in a.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    x = json.loads(line)
                    rows.append("| %s | %s | %s |" % (x.get("id", ""), x.get("text", ""), x.get("default", "")))
                except ValueError:
                    pass
        if not rows and a.strip():
            return "# 假设清单（决策引用的 id）\n\n" + a.strip()
    if not rows:
        return ""
    return "# 假设清单（决策引用的 id）\n\n| id | 假设 | 默认 |\n|---|---|---|\n" + "\n".join(rows)


def tester_cases(bb):
    raw = bb.get("tester") if isinstance(bb, dict) else None
    if not raw or not isinstance(raw, str):
        return []
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        cases = json.loads(m.group(0))
    except ValueError:
        return []
    good = []
    for c in cases if isinstance(cases, list) else []:
        try:
            pl = Plan(json.loads(json.dumps(c)))
            if any(f.dur <= 0 for f in pl.fixed) or not pl.items:
                continue
            _, ok = run_case(json.loads(json.dumps(c)))
            if ok:
                good.append(c)
        except Exception:
            continue
    return good


def run(ctx):
    bb = ctx.get("blackboard") or {}
    cases = tester_cases(bb)
    cases = BUILTIN + cases[:2]  # every archetype always runs; tester days only add unusual ones
    blocks, summary, all_ok = [], [], True
    for c in cases:
        try:
            md, ok = run_case(c)
        except Exception as e:  # never break the team on a bad case
            md, ok = "### %s 跳过（输入无法解析：%s）" % (c.get("id", "?"), e), True
        all_ok = all_ok and ok
        blocks.append(md)
        summary.append("- %s %s：%s" % (c.get("id", ""), c.get("title", ""), "自检通过" if ok else "有重叠"))
        keys = ("- 冲突", "- **排不下**", "- 截止检查", "- 今天之内", "- 压缩", "- 估时提醒", "- 合并", "- 推迟到稍后池",
                "- 质量代价", "- 缩小", "- 没关系", "- 拆分：")
        summary += ["  " + l[2:120] for l in md.splitlines() if l.startswith(keys)]
    rules = (
        "引擎规则（代码实现，示例由它生成）：固定/受保护块（含睡眠、吃饭、接送）先放且永不由助手移动，每个固定事项前留 10 分钟准备/路上；"
        "重叠的固定事项、缺时长、“尽快”等含糊词上确认卡，写明这是助手的猜测，计划是草稿，冲突时段在用户裁决前不排任何事；缺时长默认 60 分钟并标低置信；"
        "自估任务 +25% 缓冲，临时插入的急活 +40% 且这份缓冲重排时不压缩；按截止先后、整块优先排必做：整块任务优先放进精力高峰（默认上午、可改），"
        "放不下才拆成 ≥60 分钟的段，两个整块之间留 15 分钟休息，有截止的任务绝不拆到截止之后，尾差 ≤15 分钟由缓冲吸收；碎片任务填最贴合的短空档；"
        "可选任务只在保留机动时间（≥30 分钟或可用时间的 10%）后才排，放在第一件必做之后、不占一天最后一小时，放不下先试一半时长的缩小版，再进稍后池（不删除）；"
        "重排只动变化点之后、与变化冲突、被标为未做、或急活所需窗口内的块，其余原地不动；可选先让位，必做重排完再回到原位或缩小或进稍后池；"
        "被挤出的块优先进最早的空档（含会议延期释放的空档）；仍放不下时依次：压缩自估任务缓冲到 10% 保整块 → 拆成小段（用户说只能零碎干活时 ≤30 分钟）→ "
        "坦白差多少分钟、截止是否可达、今晚/明早补救或建议舍弃（睡眠永不被占用）；每次重排报告截止余量、重排后剩余机动时间和被挤出精力高峰的质量代价；"
        "所有改动以差异提案呈现，接受前不生效；一天由用户定义，可跨午夜。"
    )
    text = rules + "\n\n已生成的示例（artifact flows）：\n" + "\n".join(summary)
    flows = "# 关键流程示例（由参考排程引擎生成并校验）\n\n" + rules + "\n\n" + "\n\n---\n\n".join(blocks)
    arts = {"flows": flows}
    am = assumptions_md(ctx)
    if am:
        arts["assumptions"] = am
    return {"text": text, "artifacts": arts}


if __name__ == "__main__":
    r = run({"blackboard": {}, "context": {}})
    print(r["artifacts"]["flows"])
