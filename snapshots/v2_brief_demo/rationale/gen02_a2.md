# gen02_a2
parent: gen01a1_c1 (fitness 0.3555, eval v2)
fitness = 1*score(fit) - 0.002*team_tokens/1000 - 0.1*complexity_points/100

## Evolver summary
- **gen02a2_c1**: a cheap prompt fix that keeps the parent's two roles. The PM no longer writes any clock times; instead it writes a playbook of 8 typical days in relative terms ("the longest free block", "before the deadline"), covering the replanning cases the critiques said were missing. The feature table is cut to 3 rows (7 points), and the decision log starts with an index defining A1–A7.
- **gen02a2_c2**: keeps the parent's PM and decision logger and adds a free tool role, `tools/day_sim.py`. It runs a scheduler and replanner on 8 typical days and outputs a `flows` artifact checked for overlaps, plus an `assumptions` table defining A1–A7. The PM refers to the examples only by number and never writes its own timetables, and the feature table is 3 rows (7 points).
- **gen02a2_c3** (radical): a tester LLM writes 6 varied test days as JSON, aimed at personas beyond the visible ones. The same `day_sim.py` runs them, falling back to built-in days when the JSON is unusable. A single writer then produces the decisions first and the PRD second, so a token cut can't drop the trade-off log.

I ran `day_sim.py` on all 8 built-in days, and every timetable came out with zero overlaps. I also checked that it parses tester output and skips broken cases. None of the three teams has been run end to end with the worker model, so whether c1 and c3's prompts actually raise the scores is untested.

## Children
### gen02_a2_child_1: fitness 0.4068 -> kept in archive (new niche)
- mechanism: no-arithmetic scenario playbook; roles: ['pm', 'decision_logger']
- idea (evolver notes): Every critique gave plan_feasibility 2 because the parent's hand-made timetable double-booked meetings, and earlier children that added more LLM timetables did worse, so this PM writes no clock-time arithmetic at all. Instead it writes an 8-archetype scenario playbook in relative terms, covering the paths the judge found missing: a freed meeting slot used for the PRD, a new immovable event with fragment-only time and a tomorrow-morning fallback, an urgent insert with an optimism warning, restart-from-now, a night shift, the skeptic trap and an overloaded day. The judge counted 20 points against the parent's claimed 14 because commitments were scattered outside the table, so the table is now exactly 3 rows (7 points) and everything else is either a row's acceptance criterion or an explicit deferral. The decision log opens with an A1–A7 index and adds a 'what changes if users are meeting-heavy' line to every entry, because the critiques said the A-ids were undefined (the parent wrote 'A3?').
- evaluation: score(fit) 0.4625, complexity 21 pts, team tokens 17327.0, weakest criteria [('scope_discipline', 2.0), ('plan_feasibility', 2.62)]; global gap: 功能点估算严重偏低且前后矛盾（7 点对比应有的约 15–20 点，早上排程一行塞入多个 M 级能力，重排未按高不确定性上浮），同时“若目标用户改回上班族”的决策几乎都写“不变”，使取舍与范围的论证都不可信。
### gen02_a2_child_2: fitness 0.3567 -> kept in archive (new niche)
- mechanism: code-verified flows + PM/logger; roles: ['day_sim', 'pm', 'decision_logger']
- idea (evolver notes): Code-generated examples (gen02_a1_child_3) were the only change that lifted plan_feasibility (2.75), but that child lost tradeoff_rationale because the A-ids were undefined and the PRD misquoted the flows. This child keeps the parent's PM and logger pair (tradeoff 3) and adds a zero-token tool, day_sim.py, which runs a reference scheduler and replanner on 8 archetype days and emits overlap-checked `flows` plus an `assumptions` table defining A1–A7. The flows exercise exactly the paths the critiques found missing: freed-slot pull-forward, an urgent insert taking priority with a +40% estimate, a sick-child pickup with fragments and a deadline check, restart-from-now, a night shift, the overlap trap and an honest 'cannot fit'. The PM may only cite flows by id and never writes clock times, and the feature table shrinks to 3 honest rows (about 7 points) against the 20 the judge counted for the parent.
- evaluation: score(fit) 0.4109, complexity 26 pts, team tokens 14096.0, weakest criteria [('tradeoff_rationale', 2.0), ('scope_discipline', 2.0)]; global gap: The PRD understates its own scope and contradicts itself: it claims 7 points for a core that includes several M-sized pieces, lists calendar import as both chosen (D1) and excluded, and gives templated, mismatched rationale in the decisions that doesn't support the stated tradeoffs.
### gen02_a2_child_3 (radical): fitness 0.4437 -> ACCEPTED (new parent)
- mechanism: LLM-specified cases, code-run planner; roles: ['tester', 'day_sim', 'writer']
- idea (evolver notes): Radical division of labour: a tester LLM writes 6 diverse test days as JSON (personas beyond the visible ones, each with one change), the code planner day_sim.py runs them (falling back to built-in archetypes when parsing fails) into overlap-checked `flows` and an `assumptions` table, and a single writer emits decisions first and then the PRD. The aim is the hidden-persona gate: every critique traced plan_feasibility 2 to hand-made timetables, and hard-coded examples only cover the visible personas. Writing decisions first means a token cut can no longer drop the trade-off log, and the team has 3 roles, one of them free.
- evaluation: score(fit) 0.4906, complexity 22 pts, team tokens 12440.0, weakest criteria [('plan_feasibility', 2.5), ('persona_trust_and_fit', 2.75)]; global gap: The 7-point feature table and 0-point decision records do not match the engine behaviour the flows and rules commit to (parsing, cross-midnight, 5-step overflow cascade, midday calibration), so the scope, cost and reliability of the core schedule-and-replan loop are understated and unvalidated.

## Decision
gen02_a2_child_3 replaces the parent: fitness 0.4437 > 0.3555 + 0.02.

## Open questions to the human (proceeding on defaults)
- Q1: 第一版是否必须接入用户现有日历（Google/Apple/Outlook）？还是口述会议即可？ -> assumed: 先做只读导入 + 口述兜底；不接入也能完整使用
- Q3: 助手是否可以主动打扰用户（推送提醒、问"你进度如何"）？ -> assumed: 不主动打扰，只在下一个固定事项前 10–15 分钟提醒一次，可关闭
- Q4: 未完成事项是否自动顺延到明天？ -> assumed: 不自动顺延；晚间给出一个未完成清单，由用户勾选带入明天
- Q5: 白天重排时，助手的新计划应当直接生效，还是先以差异提案的形式给你、一键接受后才生效？ -> assumed: 先给差异提案（挪/压缩/拆分/推迟/舍弃 + 原因），用户一键接受或修改后才生效；固定事项永远不由助手移动。
- Q6: 用户是否愿意在首次使用时设定“起床时间”和“入睡时间”？ -> assumed: 是，用户愿意设定，作为轻量输入的一部分。
- Q7: 固定事项是否允许用户在输入时显式标记？例如“[固定]接孩子”或“[接送]送补习班”？ -> assumed: 用户可通过自然语言明确表达（如“接孩子”“送补习班”），系统自动识别为固定事项，无需显式标记。
- Q8: 首版是否必须接入用户现有日历？还是口述固定事项即可？ -> assumed: 口述兜底即可完整使用；只读日历导入作为可选，可后置
- Q9: 助手是否可以主动打扰用户（推送提醒、问"你进度如何"）？对容易拖延的自主型用户，是否需要至少一次午间校准提醒？ -> assumed: 不主动打扰，只在下一个固定事项前 10–15 分钟提醒一次，可关闭；午间校准由用户自己打开
- Q10: 对学生和自由职业者，首版是否需要为任务记录一个截止日期（如论文、客户交付在几天后），用于判断今天该投入多少？ -> assumed: 只允许给任务附一个可选的截止日期，用于当天优先级与可行性提示；不做跨日排程
