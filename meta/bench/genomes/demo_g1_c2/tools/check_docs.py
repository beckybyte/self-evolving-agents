"""Checks that every decision-log entry has options, a rejected-option cost, assumption ids and a fallback."""
import re

REQUIRED_TOPICS = {
    "日历": "日历接入 vs 口述",
    "顺延": "未完成是否自动顺延",
    "确认": "重排提案确认 vs 直接生效",
    "冲突": "输入冲突处理",
    "截止": "截止任务拆分与可达性",
    "跨午夜": "一天边界/跨午夜",
}


def _entries(text):
    parts = re.split(r"\n(?=#{2,4}\s*D\d+)", "\n" + text)
    return [p.strip() for p in parts if re.match(r"#{2,4}\s*D\d+", p.strip())]


def run(ctx):
    arts = ctx.get("artifacts") or {}
    decisions = arts.get("decisions", "") or ""
    issues = []

    entries = _entries(decisions)
    if not decisions.strip():
        issues.append("decisions 产物缺失：必须输出 <artifact name=\"decisions\">。")
    elif len(entries) < 8:
        issues.append(f"decisions 只有 {len(entries)} 条以 '### D<n>' 开头的条目，至少需要 8 条。")
    for e in entries:
        head = e.splitlines()[0][:40]
        if not re.search(r"(?<![A-Za-z0-9])[AQ]\d+(?!\d)", e):
            issues.append(f"{head}：没有引用任何假设 id（A1–A6 / Q1–Q4）。")
        if "选项" not in e or ("放弃" not in e and "代价" not in e):
            issues.append(f"{head}：缺少'选项'或'放弃方案的代价'。")
        if "不成立" not in e:
            issues.append(f"{head}：缺少'若假设不成立'的改动说明。")
    for kw, topic in REQUIRED_TOPICS.items():
        if decisions and kw not in decisions:
            issues.append(f"decisions 没有覆盖关键取舍：{topic}。")

    if not issues:
        return {"text": f"PASS（{len(entries)} 条决策）"}
    return {"text": "ISSUES:\n- " + "\n- ".join(issues[:20])}
