# gen01_a1
parent: brief_P0 (fitness 0.1429, eval v1)
fitness = 1*score(fit) - 0.002*team_tokens/1000 - 0.1*complexity_points/100

## Evolver summary
gen01a1_c1 - Cheap fix: the decision log moves to its own role, since the parent's single 4000-token call never emitted it in any of the 6 scenarios. A shared soul now spells out the mechanisms every critique found missing: protected non-meeting blocks, filling gaps by length, deadline splitting with a check that the deadline is still reachable, conflicts shown on a confirm card, replans offered as proposals the user accepts, and a day that can cross midnight. Scope is capped at about 14 points, down from the parent's 27.

gen01a1_c2 - Structural fix: a drafter writes the PRD, then a critic walks it through five kinds of user (meeting-heavy worker, interrupted parent, student, sceptic, night-shift worker) and lists missing rules and features to cut. A reviser writes the final PRD plus a `flows` artifact within 14 points, and a decision logger loops up to twice with `tools/check_docs.py`, a checker that requires assumption ids, rejected options, a fallback if the assumption fails, and coverage of the key trade-offs.

gen01a1_c3 - Radical: the order is reversed to decisions first, then simulated days, then the PRD. A decision architect writes the decision log, a simulator runs four very different real days against it and messages back any decision that breaks (up to two rounds), a scope cutter sizes the features under 12 points, and a compiler writes the PRD from those pieces. It uses the messages channel, and a human role asks whether replans apply directly or need one-tap confirmation, defaulting to confirmation.

## Children
### gen01_a1_child_1: fitness 0.3684 -> ACCEPTED (new parent)
- mechanism: principled PM + logger; roles: ['pm', 'decision_logger']
- idea (evolver notes): The parent never emitted the decisions artifact (missing_artifact on all 6 scenarios, artifacts_present 0.5), because one 4000-token call ran out after the PRD, so the decision log now has its own role. The soul adds scheduling mechanisms every critique said were missing: protected non-meeting blocks, a gap ladder for focus blocks, deadlines with splitting and reachability checks, surfacing conflicts instead of resolving them silently, propose-then-confirm replans, and a user-defined day boundary. Scope is capped at about 12 points (the parent's was 27): calendar sync (L) and voice input (M) are deferred and the diff view is merged into replan.
- evaluation: score(fit) 0.425, complexity 23 pts, team tokens 16822.0, weakest criteria [('plan_feasibility', 2.17), ('replan_quality', 2.33)]; global gap: The documents contradict themselves on scope and budget: the L-sized calendar read and fixed-event reminder in the decisions are missing from the feature table, the table's own points sum to 21 rather than the stated 14, and several fallbacks need history learning or background logic that the non-goals exclude, so the cost and boundary of the first release cannot be trusted.
### gen01_a1_child_2: fitness 0.2759 -> kept in archive (new niche)
- mechanism: draft-redteam-revise-check; roles: ['drafter', 'red_team', 'reviser', 'decision_logger', 'checker']
- idea (evolver notes): Every critique names a case the parent PRD never considered (overlapping meetings, sleep and child pickup as protected blocks, focus blocks, deadlines that move to tomorrow), and the decision log was never emitted. Pipeline: a drafter writes the PRD, a red-team critic walks five archetype users through a morning plan and a mid-day change and also points out features to cut, a reviser writes the final PRD and flows within a 14-point budget, and a decision logger loops with a tool checker until every entry cites assumption ids and covers the key trade-offs. The goal is replan_quality, plan_feasibility and tradeoff_rationale, the lowest criteria (2.0–2.3).
- evaluation: score(fit) 0.3563, complexity 21 pts, team tokens 29715.0, weakest criteria [('scope_discipline', 2.0), ('plan_feasibility', 2.0)]; global gap: The L-level (8 points) calendar sync takes more than half the budget with no scenario that uses it, while the real core, the minimum-change reschedule, is under-sized at S and its own examples show wrong or silent results, so the 70% core-loop budget claim does not hold.
### gen01_a1_child_3 (radical): fitness 0.1635 -> kept in archive (new niche)
- mechanism: decision-first simulation; roles: ['control_question', 'decision_architect', 'simulator', 'scope_cutter', 'prd_compiler']
- idea (evolver notes): This child reverses the parent's PRD-first flow. Decisions come first. A simulator runs concrete days against them and messages back the breakages. A scope cutter sizes the features, and only then is the PRD compiled from decisions, flows and scope. Motivation: tradeoff_rationale scored 2 and the decision log was never produced, and every scenario critique found mechanisms that broke when walked through on a real day (overlaps, cross-midnight, deadlines, focus blocks). Running simulations before writing should surface those breaks. A human role asks whether replans auto-apply or need confirmation, the question that matters most to sceptical and control-minded users, and the team proceeds on 'proposal + one-tap accept'.
- evaluation: score(fit) 0.3125, complexity 33 pts, team tokens 58004.0, weakest criteria [('replan_quality', 1.67), ('plan_feasibility', 1.83)]; global gap: The design's own worked examples contradict its rules and show the rescheduling is unreliable (fixed commute compressed, overlapping sleep and shift, meeting conflicts silently resolved, 'feasible' plans that under-schedule the task), while the feature table undercounts the real scope and has no non-goals list, and D11's calendar import is decided but never built or sized.

## Decision
gen01_a1_child_1 replaces the parent: fitness 0.3684 > 0.1429 + 0.02.

## Open questions to the human (proceeding on defaults)
- Q1: 第一版是否必须接入用户现有日历（Google/Apple/Outlook）？还是口述会议即可？ -> assumed: 先做只读导入 + 口述兜底；不接入也能完整使用
- Q2: 目标用户更偏向哪类人：上班族（会议多）、自由职业/学生（自主时间多）、还是两者都要？ -> assumed: 以会议较多的知识工作者为主，自由职业者作为次要验证对象
- Q3: 助手是否可以主动打扰用户（推送提醒、问"你进度如何"）？ -> assumed: 不主动打扰，只在下一个固定事项前 10–15 分钟提醒一次，可关闭
- Q4: 未完成事项是否自动顺延到明天？ -> assumed: 不自动顺延；晚间给出一个未完成清单，由用户勾选带入明天
- Q5: 白天重排时，助手的新计划应当直接生效，还是先以差异提案的形式给你、一键接受后才生效？ -> assumed: 先给差异提案（挪/压缩/拆分/推迟/舍弃 + 原因），用户一键接受或修改后才生效；固定事项永远不由助手移动。
- Q6: 用户是否愿意在首次使用时设定“起床时间”和“入睡时间”？ -> assumed: 是，用户愿意设定，作为轻量输入的一部分。
- Q7: 固定事项是否允许用户在输入时显式标记？例如“[固定]接孩子”或“[接送]送补习班”？ -> assumed: 用户可通过自然语言明确表达（如“接孩子”“送补习班”），系统自动识别为固定事项，无需显式标记。
