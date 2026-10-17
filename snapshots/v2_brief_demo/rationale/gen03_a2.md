# gen03_a2
parent: gen02a2_c3 (fitness 0.4437, eval v2)
fitness = 1*score(fit) - 0.002*team_tokens/1000 - 0.1*complexity_points/100

## Evolver summary
- gen03a2_c1 - Removes the tester so all 8 built-in persona days always run (so far the tester's cases pushed out the meeting-heavy, skeptic and night-shift days), fixes the engine bugs the critiques named (pickup time, cutting the urgent task's buffer, sleep as a protected block, energy-peak placement, deadline tasks split past their deadline, optional tasks giving way or shrinking to half, slack reports), and tells the single writer to add a persona guarantee matrix and concrete "if this assumption is wrong" changes while keeping 3 features at 7 points.
- gen03a2_c2 - Uses the same fixed engine and splits the writer in two: a decision-log author is checked by `decision_lint.py` (at least two options per entry, valid A/Q ids, a concrete change if the assumption fails with no "support later" dodges, a line on what changes for meeting-heavy users, a points line, all 12 topics covered) and gets one retry, then a PRD writer works from a short code-built index of the decisions so it stays consistent at low token cost.
- gen03a2_c3 - Radical: code writes the parts of the PRD that must match the engine (`assemble.py` builds the 3-feature/7-point table with a deferred list, rules R1–R11 each citing the examples where the engine applied them, the example guide and a consistency check), while two narrow LLMs write only judgement: the trade-off log, plus positioning, persona guarantees, goals and what's not being built, the evening wrap-up and risks.

## Children
### gen03_a2_child_1: fitness 0.4828 -> ACCEPTED (new parent)
- mechanism: fixed archetype engine, one writer; roles: ['day_sim', 'writer']
- idea (evolver notes): Cheap fix of the parent's two concrete failure sources. (1) The LLM tester is fired: its cases pushed the meeting-dense, skeptic and night-shift archetypes out of flows (S1/S2/S6/S9 said 'my day is never shown'), so all 8 built-in days always run, with engine bugs from the critiques fixed (pickup replaces the 17:00 one and starts so the child is reached by 15:30; urgent +40% buffer is never compressed; sleep is an in-plan protected block with drop-the-course; energy peak window; deadline tasks never split past the deadline; optionals step aside, shrink to half, avoid the last hour; slack and remaining-buffer reports after every replan). (2) The writer must add a persona guarantee matrix and concrete if-assumption-flips changes, and must keep the 3 features while explicitly deferring reminder/midday calibration/calendar import, since the honest-sizing children ballooned to 34 points.
- evaluation: score(fit) 0.525, complexity 21 pts, team tokens 10576.0, weakest criteria [('plan_feasibility', 2.88), ('tradeoff_rationale', 3.0)]; global gap: The cost and scope accounting is not credible: D12 sizes the whole engine at 7 points even though the flows commit to at least five M-level scheduling and re-planning behaviours (about 20+ points), and decisions cite undefined 'Q' anchors, so rationale and scope claims are not grounded in the stated assumptions.
### gen03_a2_child_2: fitness 0.4613 -> kept in archive (new niche)
- mechanism: linted decision log, split writers; roles: ['day_sim', 'decider', 'decision_lint', 'writer']
- idea (evolver notes): Structural change aimed at the flat tradeoff_rationale (3 in every critique) and at plan_feasibility. One 9k-token writer is split in two. A decider writes only the decision log, and decision_lint.py (code) checks it: every entry has two or more options, a choice, valid A/Q ids, a concrete if-assumption-fails change (it rejects 'support later' / 'not designed for it' dodges, which the parent's D1/D11 used), a meeting-heavy flip and a points line, and all 12 required topics are covered; a failed check sends it back once. A PRD writer then works from a compact code-built decision index, not the full log, so it stays consistent at low token cost. The tester is fired and the fixed 8-archetype engine from c1 is reused.
- evaluation: score(fit) 0.5312, complexity 25 pts, team tokens 22463.0, weakest criteria [('tradeoff_rationale', 3.0), ('scope_discipline', 3.0)]; global gap: 决策依据几乎全是用同一个参考引擎生成的示例“证明可行”，且声称的 3 个功能与 1 点估算严重低估了排程与重排的真实复杂度和不确定性，示例中的排程错误（F1、F3、F6）也暴露出可靠性未被验证。
### gen03_a2_child_3 (radical): fitness 0.3819 -> kept in archive (new niche)
- mechanism: code-assembled spec, LLM judgement only; roles: ['day_sim', 'decider', 'narrator', 'assemble']
- idea (evolver notes): Radical division of labour: code owns every part of the PRD that must agree with the engine, and LLMs write only judgement. assemble.py builds the feature table (3 features, 7 points, plus a deferred list with points), rules R1–R11 each citing the F-examples where the engine actually fired them, the example guide, a decision index and a consistency check. A decider writes the trade-off log and a narrator writes positioning, persona guarantees, goals, not-doing, the evening wrap-up and risks; the tool stitches these together. Evidence: the global gap and several critiques were internal contradictions (the claimed 7 points vs the engine's behaviour, shown plans contradicting stated rules, 'sleep protected' asserted with no mechanism), which a single LLM writer keeps reintroducing; the fixed 8-archetype engine from c1 is reused and the tester is fired.
- evaluation: score(fit) 0.4437, complexity 34 pts, team tokens 13887.0, weakest criteria [('tradeoff_rationale', 2.0), ('scope_discipline', 2.0)]; global gap: The documents contradict themselves on scope and cost: D12 says three features and a total of 3 points, while the PRD and flows commit to roughly a dozen features, and the point totals and decision rationales (including inverted 'cost of the rejected option' and a boilerplate 'unchanged for meeting-heavy users' line) are not a credible, coherent plan.

## Decision
gen03_a2_child_1 replaces the parent: fitness 0.4828 > 0.4437 + 0.02.

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
