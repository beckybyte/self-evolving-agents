"""Regenerates docs/system_design.svg:  python3 tools/draw_system_design.py docs/system_design.svg"""
import sys
from xml.sax.saxutils import escape as E
W, H = 1300, 1000
FONT = "PingFang SC, Microsoft YaHei, Noto Sans CJK SC, Noto Sans SC, Helvetica, Arial, sans-serif"
o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}">',
     '<defs>' + "".join(f'<marker id="m{n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>' for n, c in [("k", "#444"), ("o", "#c8711f"), ("g", "#777")]) + '</defs>',
     f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
P, B, G, O, HU = "#6f4fd1", "#2f6fcf", "#23885a", "#c8711f", "#777"

def text(x, y, s, size=12.5, color="#333", weight="400", italic=False):
    st = ' font-style="italic"' if italic else ""
    o.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}"{st}>{E(s)}</text>')

def band(y, h, fill, stroke, title, note=""):
    o.append(f'<rect x="16" y="{y}" width="960" height="{h}" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
    text(34, y + 27, title, 17, stroke, "700")
    if note:
        o.append(f'<text x="{34 + note[0]}" y="{y + 27}" font-size="12.5" fill="#666">{E(note[1])}</text>')

def box(x, y, w, h, title, lines, stroke, fill="#ffffff"):
    o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
    text(x + 12, y + 23, title, 14.5, "#222", "700")
    for i, l in enumerate(lines):
        text(x + 12, y + 45 + i * 19, l)

def arrow(pts, m="k", color="#444", width=1.6, head=True):
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    me = f' marker-end="url(#m{m})"' if head else ""
    o.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{me}/>')

# ---------------- L3  (16..196)
band(16, 180, "#f4f0ff", P, "L3 慢循环 · 框架层进化（改 src/sea 本身）", (370, "每几次运行 · 更强的 Claude Code 会话 · 评估器和数据是受保护面"))
y3 = 58
box(34, y3, 180, 122, "信号", ["evolver 的愿望清单", "自动检测器（无效 genome、", "  judge 无区分度、学习全拒…）", "瓶颈诊断第 5 层 · 你"], P)
box(230, y3, 140, 122, "提案 backlog", ["P001 – P006", "证据 + 验收标准", "按次数 × 影响排序"], P)
box(386, y3, 180, 122, "框架进化者", ["Claude Code 会话", "独立 git worktree", "可见会话 / headless", "改受保护路径 → 拒绝"], P)
box(582, y3, 220, 122, "元评测（评估器取自基线）", ["A  单测 + 冻结安全测试", "B  假 LLM 的 prompt 回归", "C  短进化：新框架 vs 旧框架", "预算 $15 / 1 小时"], P)
box(818, y3, 140, 122, "evolve/main", ["framework_log.md", "合进 main：你决定", "改动作用于 L0–L2"], P)
for a, b in [(214, 230), (370, 386), (566, 582), (802, 818)]:
    arrow([(a, y3 + 61), (b, y3 + 61)])
# ---------------- L2  (236..422)
band(236, 186, "#edf4ff", B, "L2 中循环 · 团队进化（每代）", (250, "greedy 父代 · 子代胜出就替换（recursive），否则带反馈重试（iterative）"))
y2 = 280
box(34, y2, 214, 126, "隔离工作区", ["父代 genome + 每角色记忆", "fit 报告 · judge 的批评", "失败子代的压缩反馈", "看不到 holdout / test"], B)
box(264, y2, 206, 126, "Evolver（冻结 Claude）", ["读证据、诊断失败", "写 k = 3 个子代 genome", "其中 1 个必须激进", "notes：改了什么、为什么"], B)
box(486, y2, 196, 126, "子代准备", ["继承同名角色的记忆", "+ 共享品味 _shared.md", "可选：门控学习一轮", "工具代码静态安全检查"], B)
box(698, y2, 260, 126, "选择与记录", ["子代 > 父代 + margin → 替换", "否则从父代重试（带反馈）", "所有子代进 MAP-Elites 档案", "每代写 rationale + design_log"], B)
for a, b in [(248, 264), (470, 486)]:
    arrow([(a, y2 + 63), (b, y2 + 63)])
# ---------------- L1  (462..632)
band(462, 170, "#ecf8f0", G, "L1 一次团队运行", (590, "快循环：角色改自己的笔记，holdout 上赢了才保留"))
y1 = 506
box(34, y1, 290, 112, "Genome v2（唯一被进化的）", ["roles：llm | tool | human", "workflow：顺序 / 有界循环", "channels：blackboard / messages", "memory/<role>.md（门控学习）"], G)
box(340, y1, 300, 112, "Executor 跑一个 episode", ["按 workflow 调角色（本地 vLLM）", "tool 角色：子进程、60 秒上限", "ASK_HUMAN：发问题，按默认继续", "记录每个角色的 token"], G)
box(656, y1, 302, 112, "产出", ["产出物：PRD · 决策记录 · 流程", "（benchmark 模式：答案）", "假设清单 = 未回答的问题", "团队 token → 成本项"], G)
for a, b in [(324, 340), (640, 656)]:
    arrow([(a, y1 + 56), (b, y1 + 56)])
# ---------------- L0  (672..908)
band(672, 236, "#fff5ea", O, "L0 Task 插件 · 自建评估（冻结）", (280, "另有 ground-truth 插件用于回归：calendar · MATH · DROP · AppWorld"))
y0 = 716
box(34, y0, 150, 112, "产品 brief", ["模糊的 PM 式目标", "briefs/*.md", "没有数据", "没有标准答案"], O)
box(200, y0, 220, 112, "Framer（Claude）", ["复述目标、列假设", "向你提问（不等待）", "建评估 goal/vN", "你回答后修订 → vN+1"], O)
box(436, y0, 250, 112, "goal/vN（带版本）", ["persona · 场景（fit | holdout）", "rubric：取舍理由、MVP 克制…", "复杂度估算规则", "改版后父代先重评"], O)
box(702, y0, 256, 112, "Judge（Claude）", ["以 persona 走一遍场景打分", "全局项 + 功能复杂度点数", "只看产出物，不看 genome", "fitness = 分 − λ·token − λ·复杂度"], O)
for a, b in [(184, 200), (420, 436), (686, 702)]:
    arrow([(a, y0 + 56), (b, y0 + 56)])
# ---------------- cross-layer
arrow([(930, y2), (930, 216), (124, 216), (124, y3 + 122)])
text(420, 211, "愿望清单 · 检测器信号 → 提案", 12, "#444", italic=True)
arrow([(367, y2 + 126), (367, 446), (290, 446), (290, y1)])
text(186, 441, "写子代 genome", 12, "#444", italic=True)
arrow([(584, y2 + 126), (584, y1)])
text(592, 446, "评估每个子代", 12, "#444", italic=True)
arrow([(860, y1 + 112), (860, y0)])
text(868, 658, "产出物", 12, "#444", italic=True)
arrow([(958, y0 + 70), (988, y0 + 70), (988, y2 + 100), (958, y2 + 100)], "o", O, 2.2)
o.append('<text x="999" y="600" font-size="12.5" font-weight="700" fill="%s" font-style="italic" transform="rotate(-90 999 600)">fitness</text>' % O)
# ---------------- human column
o.append(f'<rect x="1004" y="16" width="280" height="892" rx="14" fill="#fafafa" stroke="#888" stroke-width="1.5"/>')
text(1022, 43, "你（人）", 17, "#333", "700")
text(1100, 43, "随时介入，系统从不停下来等你", 12.5, "#666")
box(1018, y3, 252, 122, "审批", ["批准改评估器的提案", "决定合进 main"], HU)
box(1018, y2, 252, 126, "读进化记录", ["rationale/genXX.md：", "  每代改了什么、为什么", "design_log.md：设计演化史", "framework_log.md"], HU)
box(1018, y1, 252, 112, "收件箱 inbox/", ["系统的提问 + 默认答案", "系统按默认继续，不等你", "随时回答：sea.human answer", "或打分：sea.human rate"], HU)
box(1018, y0, 252, 112, "你的输入", ["写 brief（唯一必需的输入）", "你的回答和评分", "在代际边界折回", "→ Framer 修订评估"], HU)
arrow([(1018, y3 + 61), (958, y3 + 61)], "g", HU)
arrow([(958, y2 + 30), (1018, y2 + 30)], "g", HU)
arrow([(958, y1 + 30), (1018, y1 + 30)], "g", HU)
arrow([(1144, y1 + 112), (1144, y0)], "g", HU)
arrow([(1144, y0 + 112), (1144, 880), (109, 880), (109, y0 + 112)], "g", HU)
arrow([(310, 880), (310, y0 + 112)], "g", HU)
text(420, 875, "brief → 产品 brief；回答与评分 → Framer（修订评估）", 12, "#555", italic=True)
# ---------------- legend
text(24, 944, "三层循环", 13.5, "#222", "700")
text(100, 944, "快 · 每批经验：角色改自己的笔记（门控）   ｜   中 · 每代：evolver 改团队   ｜   慢 · 每几次运行：Claude Code 改框架（元评测把关）", 13, "#333")
text(24, 972, "尚未实现（只有设计）", 13.5, "#222", "700")
text(170, 972, "§2.1 能力开关：记忆 / 学习 / 工具按角色开关、运行时自选工具 · §7 瓶颈诊断 + 目标放大 · 提案 P002–P006", 13, "#555")
o.append('</svg>')
open(sys.argv[1], "w").write("\n".join(o))
