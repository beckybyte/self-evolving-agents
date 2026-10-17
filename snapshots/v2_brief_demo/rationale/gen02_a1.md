# gen02_a1
parent: gen01a1_c1 (fitness 0.3555, eval v2)
fitness = 1*score(fit) - 0.002*team_tokens/1000 - 0.1*complexity_points/100

**Evaluation revised v1 -> v2 after human input; parent re-scored 0.3684 -> 0.3555.**

## Evolver summary
The three children are written. All three genomes parse, both tool scripts run, and the child_3 simulator's output passes child_2's overlap checker with no overlaps in any of its 14 time tables. None of the children has been through the judged evaluation yet, so whether any of them beats the parent is untested.

What went wrong in the parent: its PRD had one generic worked example, and that example put tasks inside its own fixed meetings and misread a task duration the user had given. The judge used this as evidence the scheduler couldn't be trusted, and plan_feasibility got 2/5 in all 8 scenarios. Separately, the feature table claimed 14 points, but the judge counted 20 because features committed elsewhere in the text (noon calibration, cross-midnight day, reminder, voice) were missing from it.

- **gen02a1_c1** - The same two roles with rewritten prompts: five short worked examples, one per user type, written as time tables with a check against overlaps, a confirmation card that no longer blocks the plan, and a feature table of 5 features (about 11 points) that must include everything committed.
- **gen02a1_c2** - A draft-check-fix team: the PM writes rules and the honest feature table, a separate writer produces 6 example days as time tables, a tool finds every overlap, and a fixer repairs only what the tool flags (up to 2 rounds, stopping on ALL_OK).
- **gen02a1_c3** (radical) - A tool role runs real scheduling and replanning code on 8 typical days, including night shift, an overloaded day and an urgent insert, and outputs conflict-free examples plus the rule summary. The PM and decision logger only write rules, scope and trade-offs that cite those cases, so the examples cost no LLM tokens.

c2 adds two LLM calls on top of c1, so its token cost could eat its gain.

Files are in `child_1/`, `child_2/` and `child_3/`:
- child_1/genome.yaml
- child_2/genome.yaml
- child_2/tools/timeline_check.py
- child_3/genome.yaml
- child_3/tools/day_sim.py

## Children
### gen02_a1_child_1: fitness 0.2959 -> kept in archive (new niche)
- mechanism: consistent-examples PM + logger; roles: ['pm', 'decision_logger']
- idea (evolver notes): Same two-role team, with the prompts rewritten against the critiques. All 8 critiques gave plan_feasibility 2 because the PRD's one generic example double-booked fixed meetings, called a 3-hour task 'missing duration', and replanned in ways that made things worse. The prompts now require short worked examples, one per archetype (no-structure student, interrupted parent, urgent insert, overlapping-meeting trap, night shift), written as time tables with a fixed-first, list-gaps, then-place self-check. The judge also counted 20 points against the parent's claimed 14 (noon calibration, cross-midnight, reminder and voice were committed in the text but missing from the table), so the PRD's table now has to list every first-release capability and everything else is deferred explicitly. The confirmation card no longer blocks the plan.
- evaluation: score(fit) 0.3547, complexity 11 pts, team tokens 23888.0, weakest criteria [('plan_feasibility', 1.88), ('non_calendar_differentiation', 2.0)]; global gap: The worked examples that should prove the core value (feasibility judgment and minimal-change reschedule with explanation) are internally inconsistent, with overlaps, wrong buffer arithmetic, an identical 'diff' and a scheduled shift conflict. The PRD also under-sizes the uncertain scheduling/reschedule items at 5 points against a 15–20 budget.
### gen02_a1_child_2: fitness 0.2829 -> kept in archive (new niche)
- mechanism: draft-check-fix timelines; roles: ['pm', 'flow_writer', 'check', 'fixer', 'decision_logger']
- idea (evolver notes): Structural change: the parent's examples double-booked fixed meetings, which a judge read as proof the scheduler was unsafe (plan_feasibility 2/5 in all 8 scenarios). The worked examples now come from a dedicated flow_writer (6 archetype days as strict time tables), a deterministic timeline_check tool finds every overlap, and a fixer repairs only what the checker flags (at most 2 rounds, stopping on ALL_OK). The PM keeps rules and an honest 5-feature table (about 11 points, everything committed is counted, because the judge sized the parent at 20 against a claimed 14). The soul is the same as c1's.
- evaluation: score(fit) 0.3604, complexity 11 pts, team tokens 33249.0, weakest criteria [('plan_feasibility', 2.5), ('persona_trust_and_fit', 2.75)]; global gap: The cost accounting is wrong and understated (5 points claimed against 11 actual, and 18 rather than 8 in the office-worker scenario), while the three M features each bundle several sub-features with high replan-quality uncertainty that is not sized up. Together with the undefined A1–A7 assumptions, the scope and trade-off claims cannot be verified.
### gen02_a1_child_3 (radical): fitness 0.2406 -> kept in archive (new niche)
- mechanism: executable-spec simulator; roles: ['sim', 'pm', 'decision_logger']
- idea (evolver notes): Radical change: the worked examples are no longer written by an LLM. A tool role (day_sim.py) holds a deterministic reference scheduler and replanner covering fixed/protected blocks, buffers, focus versus fragment placement, pull-forward into freed slots, deadline-priority yielding, the later pool, honest 'cannot fit' messages and cross-midnight days. It runs that code on 8 archetype days (meeting-dense, interrupted parent, deep-work researcher, night shift, no-structure student, urgent insert, overlapping-meeting trap, overloaded day) and emits a verified `flows` artifact plus a short rule summary. The cause is that every critique traced plan_feasibility 2/5 to the parent's hand-made examples double-booking meetings, and a 30B worker cannot be trusted with time arithmetic; the PM and logger now only write rules, scope and trade-offs that cite the cases, at zero extra LLM tokens for the examples.
- evaluation: score(fit) 0.2885, complexity 11 pts, team tokens 18453.0, weakest criteria [('tradeoff_rationale', 2.0), ('plan_feasibility', 2.75)]; global gap: The tradeoffs are not tied to the target-user assumptions (A1–A7). The PRD never says what alternatives were rejected, what they cost, or what would change if the assumptions fail, and its own 'why not a calendar' evidence misquotes the flows.

## Decision
No child beat the parent (best 0.2959 vs 0.3555 + 0.02); retry from the parent with feedback.

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
