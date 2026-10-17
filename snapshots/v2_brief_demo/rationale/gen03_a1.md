# gen03_a1
parent: gen02a2_c3 (fitness 0.4437, eval v2)
fitness = 1*score(fit) - 0.002*team_tokens/1000 - 0.1*complexity_points/100

## Evolver summary
gen03a1_c1 - Keeps the parent's tester → engine → writer pipeline with a rebuilt engine: sleep is protected, urgent tasks keep their buffer, every deadline task reports remaining work and slack, and the engine adds transition buffers, a focus window and a pickup-deadline fix, and always runs all 8 persona archetypes. The writer has to size every feature honestly and say concretely what changes if an assumption turns out wrong.

gen03a1_c2 - Uses the same rebuilt engine but drops the LLM tester. A cheap persona advocate walks 8 kinds of user through the engine's example days (`flows`) and writes a `personas` artifact of remaining doubts and rule fixes, which the writer must fold into the decisions and PRD.

gen03a1_c3 - Radical: no engine and no clock-time timetables. A scoper and a hostile persona panel argue over a message channel for up to 2 rounds about scope, decisions and rules. A writer then produces decisions, PRD and `flows` written as dialogue scripts with relative durations only, so the documents cannot contradict themselves on arithmetic.

## Children
### gen03_a1_child_1: fitness 0.425 -> kept in archive (new niche)
- mechanism: archetype engine, honest scope; roles: ['tester', 'day_sim', 'writer']
- idea (evolver notes): Same 3-role topology, but the engine is rebuilt on the critique evidence. Sleep is now a protected block placed first, and the engine drops the course rather than the sleep (S9). Urgent inserts keep their +40% buffer and every deadline task reports remaining work and slack (S12, S4). The parent's 'pickup by 15:30' contradiction is fixed (S4), and there is a 10-minute transition before every appointment (S1/S5). A stated or visible-default energy window replaces 'earliest gap', with focus capped at 120 minutes (S11). All 8 built-in archetypes (meeting-dense, skeptic traps, night shift) now always run, where tester cases used to push them out, and the tester adds at most 2 unusual days. The writer must size every committed capability honestly (the judge counted 22 points against the 7 claimed) and must write concrete 'if A1 flips' changes.
- evaluation: score(fit) 0.4875, complexity 34 pts, team tokens 14244.0, weakest criteria [('scope_discipline', 2.0), ('plan_feasibility', 2.88)]; global gap: The first release has no consolidated feature table, no explicit not-doing list and no total against the 15–20 point budget, and it keeps optional calendar import (an L-size item the cost model says to defer) with a rationale that contradicts the student/freelancer target user.
### gen03_a1_child_2: fitness 0.4169 -> kept in archive (new niche)
- mechanism: engine plus persona advocate; roles: ['day_sim', 'advocate', 'writer']
- idea (evolver notes): Uses the rebuilt engine from c1 (protected sleep, urgent buffer kept, slack and remaining-work reports, transition buffers, energy window, a pickup-by-deadline fix). The LLM tester is dropped: its 3k tokens mostly pushed the persona archetypes out of the flows. In its place a persona advocate runs before the writer and walks 8 kinds of user through the code-made flows. The advocate writes a `personas` artifact (what each user fears, what is guaranteed, the residual doubt, and a concrete rule or decision fix), and the writer has to fold those fixes in. Every fit critique was a persona saying 'my day is not shown or not trusted' (persona_trust 2-3), so the hypothesis is that a cheaper and more targeted persona pass beats generating more test days.
- evaluation: score(fit) 0.4813, complexity 34 pts, team tokens 15181.0, weakest criteria [('scope_discipline', 2.0), ('plan_feasibility', 2.88)]; global gap: 
### gen03_a1_child_3 (radical): fitness 0.3478 -> kept in archive (new niche)
- mechanism: persona-panel debate, policy flows; roles: ['scoper', 'panel', 'writer']
- idea (evolver notes): Radical: no code planner and no clock-time timetables. Most fit critiques penalised specific contradictions inside hand-made or engine-made timetables (a pickup after the deadline, sleep missing, the urgent buffer cut to 10%), so this team commits to policies and dialogue-level flows, and the judge checks rules instead of arithmetic. A scoper drafts the decision log and an explicit cut list. A hostile persona panel then attacks it over a message channel, one persona at a time, including users the scoper did not think of, for up to 2 rounds. The final writer turns the surviving decisions into a PRD and `flows` written as user utterance → confirmation card / diff proposal scripts with relative durations only.
- evaluation: score(fit) 0.4141, complexity 24 pts, team tokens 21125.0, weakest criteria [('plan_feasibility', 2.0), ('replan_quality', 2.38)]; global gap: The cost model and decisions are internally inconsistent and the sample flows contradict the rules (meeting-gap scheduling vs. D10, arithmetically incoherent diffs, non-minimal replans), so the central 'trustworthy judgment and minimal-change replanning' value is asserted rather than demonstrated, and the 12-point estimate undercounts the core scheduling work and lacks reliability uplift and a ver

## Decision
No child beat the parent (best 0.425 vs 0.4437 + 0.02); retry from the parent with feedback.

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
