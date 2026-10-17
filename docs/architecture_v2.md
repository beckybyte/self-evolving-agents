# Architecture v2：从 benchmark genome 进化到 open-ended 产品设计

状态：§1-§6、§9 设计已确认并实现（分支 `v2-open-ended`，基于 commit 1517762）。**§7 瓶颈诊断 + 目标放大、§8 框架层进化是确认后追加的设计，尚未实现**。

## 0. 一句话

v1 的问题是“任务 = 数据 + 程序 scorer”，所以 genome、evaluator、evolver 提示都长成了 benchmark 的形状。
v2 把三件事拆开：**Genome**（团队怎么组织，和任务无关）、**Task 插件**（输入从哪来、怎么评、evolver 能看到什么）、
**Evolver 循环**（iterative/recursive、档案、门控学习、ask_human、取舍记录）。产品 brief 模式和 benchmark 模式都只是 Task 插件；
产品 brief 的特殊之处是**评估本身是系统自建的、带版本的、会随人的回答修订**。

```
L2  Evolver 循环 (sea.evolve)   frozen Claude; 读 TaskView 给的文件 -> 写 k 个子代; 选择器 (greedy | archive)
        ├─ HumanChannel        ask_human: 发问题 + 默认答案, 不等待; 回答在代际边界折回
        └─ Rationale log       每代 genXX.md: 改了什么、凭什么证据、为什么接受/拒绝
L1  Genome v2 (唯一可变的东西)   roles[llm|tool|human] · workflow · channels · artifacts · memory/<role>.md
L0  Task 插件 (冻结)
        ├─ BenchmarkTask       calendar / math / drop: data/splits + calevo.tasks.score (ground truth)
        ├─ AppWorldTask        子进程调用 appworld/run_genome.py (保留原 genome 格式)
        └─ BriefTask           brief.md -> Framer 建评估 (假设/persona/场景/rubric/成本) -> Judge 打分
```

## 1. 代码布局

新建包 `src/sea/`（self-evolving agents），**`src/calevo/` 原样保留**：它变成 benchmark 插件的打分库
（verifier / solver / tasks / official_grader），旧的 `calevo.runner` / `calevo.evolve` 继续能跑，作为对照基线。

```
src/sea/
  genome.py        Genome v2 加载/校验 + v1 genome 适配器 (final/input/{conversation} -> v2)
  executor.py      在一个 Episode 上跑团队: workflow、blackboard/消息、artifact 收集、ask_human、token 计数
  llm.py           后端: openai 兼容 (vLLM 8100-8102) | claude CLI (claude -p)，统一返回 text + usage
  human.py         HumanChannel: inbox/questions.jsonl, inbox/answers.jsonl, 去重/限流
  fitness.py       FitnessSpec: 若干加权项 (score 项, -λ_tok·tokens, -λ_cx·complexity, ...)
  evolve.py        通用进化循环 (iterative/recursive, patience, resumable state.json)
  archive.py       Selector 接口: GreedySelector (默认) + MapElitesArchive (描述子 + 网格, 先只记录)
  learn.py         门控的每 agent 记忆学习 (fast loop)
  tasks/
    base.py        Task 协议、Episode、EvalReport
    benchmark.py   calendar / math / drop 插件
    appworld.py    AppWorld 插件 (包装现有子进程)
    brief/
      task.py      BriefTask
      framer.py    复述目标、假设、问题、建/改评估 (带版本)
      judge.py     rubric × 场景打分 + 复杂度估计
briefs/day_planner.md            示例 brief
genomes/v2/brief_P0/genome.yaml  brief 模式的起始团队 (单个 PM 角色)
```

## 2. Genome v2

```yaml
schema: 2
id: gen02a1_c3
parent: gen01a1_c1
notes: "改了什么 + 依据哪条失败证据 (2-4 句)"
soul: "所有 LLM 角色共享的 system prompt (可空)"

channels: blackboard            # blackboard (默认: 所有角色读写一个共享字典)
                                # messages   (每个角色有收件箱, 角色输出里用 @to:<role> 段落定向发送)
roles:
  - name: researcher
    kind: llm                   # llm | tool | human
    model: $WORKER
    reads: [input, artifacts]   # 上下文选择器: input / blackboard / blackboard.<key> / artifacts / artifact.<name>
                                #               / inbox / assumptions / human_answers
    prompt: |                   # 模板占位符 = reads 里的名字, 如 {input} {blackboard.critic} {artifact.prd}
      ...
    writes: blackboard          # blackboard (默认, key=角色名) | artifact:<name> | message:<role>
    max_tokens: 2048
  - name: estimate
    kind: tool
    tool: tools/estimate.py     # run(ctx) -> {"text": ..., "artifacts": {...}?}; v1 的 run(bb, messages) 自动兼容
  - name: stakeholder
    kind: human                 # 不是真等人: 返回该问题的最新人类回答, 没有则返回声明的默认值并记为假设
    question: "{blackboard.pm_questions}"

workflow:                       # 拓扑
  steps: [researcher, designer, critic, designer]   # 顺序; 同名角色可多次出现 (= 迭代修改)
  # 或 loop: {body: [designer, critic], max_rounds: 3, stop_when: "critic says APPROVE"}

artifacts:                      # 团队必须产出的东西; 缺失 = 该项 0 分
  - {name: prd, format: markdown}
  - {name: decisions, format: markdown}   # 取舍记录 (见 §5)
outputs: {answer: blackboard.final}       # benchmark 插件读这个 (= v1 的 final)
```

要点：
- **角色三类**：`llm`（提示 + 模型）、`tool`（evolver 写的 Python，子进程 60s 限时）、`human`（经 HumanChannel，非阻塞）。
- **任何 LLM 角色都能 ask_human**：输出里写 `ASK_HUMAN: <问题> | DEFAULT: <默认答案>`，executor 抽出来发到 inbox，
  正文里按默认继续，并把默认写进 `assumptions`。每次运行限 3 个问题、跨代去重。
- **通信方式**由 `channels` + `reads/writes` 决定，blackboard 是默认，messages 用于需要“定向讨论”的拓扑。
- **v1 适配器**：`final: X` → `outputs.answer: blackboard.X`；`input: native` → 该角色以原始 chat 消息运行（executor 保留 native 模式）；
  `input: transcript` + `{conversation}` → `reads: [input, blackboard]`、`{conversation}` 作为 `{input}` 的别名。
  所以 `genomes/P0`、`genomes/S0`、`snapshots/` 里所有 v1 genome 不改一字就能在 v2 executor 上跑——这是回归检查的基础。
- AppWorld 的环境循环（`act`、每步执行代码）**本轮不并入** v2 executor：AppWorldTask 声明 `executor: external`，
  继续用 `appworld/run_genome.py` 和它自己的 schema。将来要统一的话，环境循环 = `workflow.loop` + 一个 `kind: env` 角色。

## 3. Task 插件接口

```python
class Task(Protocol):
    name: str
    def describe(self) -> str                      # 给 evolver 的任务描述 (替代 CALENDAR_TASK / tasks_desc/*.md)
    def schema_doc(self) -> str                    # 给 evolver 的 genome/工具文档 (替代 SCHEMA_DOC/TOOL_DOC)
    def splits(self) -> dict                       # {"fit": [...], "holdout": [...], "test": [...]}
    def evaluate(self, genome_dir, split, out) -> EvalReport   # 默认实现: episodes(split) -> executor -> score
    def episodes(self, split) -> list[Episode]
    def score(self, episode, run_result) -> dict   # {"score": 0-1, "reason", "detail", ...}
    def workspace_files(self, parent_eval) -> dict[str, str]   # 放进 evolver 工作区的文件 (只来自 fit split)
    def refresh(self, human_answers) -> bool       # 代际边界调用; 返回 True 表示评估改版 -> 父代要在新版上重评
```

- **fit**：evolver 能看报告和失败样本；**holdout**：只用于门控（记忆更新、可选的最终选择），evolver 看不到；
  **test**：只在 `sea.report` 里跑。隔离规则和 v1 一样：工作区里只有 `workspace_files()` 返回的东西。
- `BenchmarkTask("calendar", fit={"dev_in":0.6,"ood_dev":0.4}, holdout="dev_v2", test=[...])`：episodes 来自 `data/splits/*.jsonl`，
  score 直接调 `calevo.tasks.score`，failures 视图沿用 v1 的 `failing_examples`。MATH/DROP 同理（`tasks_desc/*.md` 作为 describe）。
- `AppWorldTask`：重写 `evaluate()` 调子进程（即现在的 `evaluate_appworld`），`schema_doc()` 返回 `appworld/genome_schema.md`。

## 4. BriefTask：没有数据时怎么评

### 4.1 Framing（每个评估版本一次，由冻结的 Framer 做，evolver 看不到其中的 holdout 部分）
输入 `brief.md`，产出 `runs/<run>/goal/v<N>/`：

| 文件 | 内容 |
|---|---|
| `restatement.md` | 用自己的话复述目标、目标用户、成功的样子、明确不做什么 |
| `assumptions.jsonl` | `{id, text, default, confidence, status: open/confirmed/rejected, source}` |
| `questions` | 最多 5 个最影响取舍的问题 → `HumanChannel.ask()`（附默认答案），**不等待** |
| `personas.jsonl` | 6-10 个 persona（含边缘用户、反对者），每个有目标、约束、痛点 |
| `scenarios.jsonl` | 每个 persona 1-2 个具体场景；**按 2:1 切成 fit / holdout** |
| `rubric.yaml` | 5-7 条代理指标，每条有权重、1-5 分锚点描述。例：核心任务覆盖、取舍是否说清理由、MVP 范围克制、可验证的验收标准、对 persona 冲突的处理、风险与未决问题 |
| `cost_model.md` | 复杂度估计规则：Judge 把 PRD 里每个功能估成 S/M/L（1/3/8 点），总点数进 fitness 惩罚项 |
| `CHANGELOG.md` | 本版相对上版改了什么、因为哪条人类回答 |

### 4.2 一次评估
1. 团队在 Episode = {brief, restatement, 当前 assumptions, 已有人类回答} 上**跑一次**，产出 artifacts（PRD、decisions、flow …）。
2. Judge 对每个场景：“以这个 persona 走一遍这个设计，按 rubric 每条打 1-5 分，指出最具体的一个失败点”；
   另有一次全局调用估复杂度。Judge 只看 artifacts，**不看 genome 的 notes、不看团队内部讨论**（防止自我辩护）。
3. 报告：每条 rubric 的均值、每个场景的分数和失败点、复杂度点数、团队 token、Judge token（单独记，不进 fitness）。
4. `workspace_files()` 给 evolver：fit 场景的 judge 批评（相当于“失败样本”）、rubric、assumptions、open questions、上一代 decisions。

### 4.3 Fitness
```
F = Σ_c w_c · mean_{s∈fit} judge(c, s)/5      # rubric 加权分, 0-1
  + w_h · human_rating                          # 如果我对这一代打过分 (inbox/ratings.jsonl), 否则该项权重为 0
  − λ_tok · team_tokens/1000
  − λ_cx  · complexity_points/100
```
- 接受条件：`F_child > F_parent + margin`（默认 0.02，抵抗 judge 噪声）；评估改版后父代先在新版上重评，再比较。
- 防 Goodhart：rubric 有锚点；holdout 场景只用于门控；复杂度惩罚压住“功能堆砌”；Judge 和 Framer 与团队用不同模型（可配置）。

### 4.4 评估随人的回答修订
人在 `inbox/answers.jsonl` 回答（或 `python -m sea.inbox answer Q3 "..."`），代际边界 `BriefTask.refresh()`：
相关假设改为 confirmed/rejected → Framer 生成 `goal/v<N+1>/`（可能改 rubric 权重、增删 persona、改场景）→ 父代在 v<N+1> 上重评 →
回答同时写进 `memory/_shared.md`（团队“品味”，所有角色可读）。基于被推翻的假设做出的取舍，会在下一代 evolver 工作区里被点名“需要复查”。

## 5. 取舍记录（每代保留“为什么”）

两层：
- **团队层**：brief 模式要求 artifact `decisions`，每条 `功能/问题 · 备选 · 选择 · 理由 · 依赖的假设 id`。Judge 有一条 rubric 专门评它。
- **进化层**：`runs/<run>/rationale/genXX.md`，自动生成：父代、每个子代的 notes（evolver 写的动机）、分数分解、judge 关键批评、
  接受/拒绝及原因、本代用到的假设和收到的回答。被接受的一代把它的 decisions 追加到 `runs/<run>/design_log.md`，形成一条可读的设计演化史。

## 6. 保留的机制，以及它们在 v2 里的位置

| 机制 | v1 位置 | v2 位置 |
|---|---|---|
| greedy 父代 + 失败子代压缩反馈 (iterative) | `calevo.evolve.run` | `sea.evolve`，反馈文本由 `Task.summarize_child()` 生成（brief 模式 = 分数分解 + 最大的 2 条批评） |
| 子代胜出替换父代 (recursive) | 同上 | `GreedySelector`（默认） |
| 每 agent `memory/<role>.md` | `calevo.learn` | `sea.learn`，**加门控**：反思生成候选笔记 → 在 holdout 上比较新旧笔记 → 胜出 margin 才采纳，否则回滚并记录被拒笔记 |
| token 成本项 | `fitness()` 写死 | `FitnessSpec` 的一项 |
| evolver 工作区隔离 | `workspaces/<tag>/` | 同，内容由 `Task.workspace_files()` 决定，加一条静态检查：子代 tool 代码不得引用 `goal/`、`data/splits`、holdout 文件名 |

## 7. 瓶颈诊断 + 目标放大（evolver 之上的固定环节，默认开启）

**原则**：目标窄的时候，最省力的解就是过拟合（写死规则、背模板）；只有目标更大、更多样时，“写死”比“做通用机制”更贵，
系统才会被逼出泛化。所以进化停滞或分数饱和时，不是继续进化、也不是直接停，而是先**诊断瓶颈在哪一层**，再决定动作。

**v1 的证据（calendar-evo）**
- 日历 v1 被 regex + 求解器刷到 1.0；换成自由措辞、带冲突的难数据后暴露真实短板（留出偏好类型 0.415），再进化到 0.549 —— 瓶颈在**数据层**。
- fitness 只含 A 类偏好时，在从未见过的 B 类偏好上 0.625；fitness 加入 C 类（偏好种类更多）后，同样未见的 B 类升到 0.838 —— 瓶颈在**目标层**，目标变大带来泛化。
- 强 token 惩罚（λ=0.02）把 LLM 完全进化掉，变成 0 token 的 regex（同分布满分，新措辞 0.41）；降低 λ 后保留 LLM、更稳 —— 瓶颈在**目标函数**。
- 在模糊产品 brief 上第 2、3 层最常见（代理指标太窄、自建场景太单一），v2 已经撞到一次：72B judge 对 baseline PRD 打 0.97（“设计完美”），
  校准后又对 6 个场景给出完全相同的 0.475 —— 评估本身没有区分度，再进化团队毫无意义。

**触发**（任一满足；在 `sea.evolve` 里替代“patience 用完就停”）
- 停滞：连续 `patience` 代没有子代被接受；
- 饱和：父代 fit 分 ≥ 0.98，或 rubric 大部分条目在天花板（≥4.5/5）；
- 坍缩：LLM 角色数降到 0（v1 的 regex 现象），或 token/复杂度骤降而分数不变；
- 过拟合信号：fit 与 holdout 分差 > 0.15。

**诊断者**：冻结的 Claude（`sea.diagnose`），在 evolver 工作区**之外**运行，可以看 holdout 的汇总数字，但它的输出给 evolver 时只保留结论和 fit 侧证据，不泄露 holdout 场景内容。
它先拿到一份**程序计算的证据包**，再给出判断，不能只凭印象：

| 层 | 典型动作 | 证据（自动计算，进证据包） |
|---|---|---|
| 1 团队结构（缺角色、协作方式不对） | 继续进化团队；可强制下一代 2 个激进子代 | 档案里有结构不同但分数接近的精英；失败分散、和某个角色的输出强相关；子代多样但没赢 |
| 2 数据 / 场景（太窄、太模板化、分布单一） | 扩充或改造评估场景：更多样的 persona、措辞、冲突情形；新评估版本 | fit 饱和而 holdout 低（分差）；失败集中在少数场景类型；persona/场景彼此相似（Framer 自评的覆盖矩阵）；换措辞重写同一场景后分数大跌 |
| 3 目标 / 评估（目标太窄可被刷穿、评估有漏洞、代理指标不代表真实目标） | 放大目标、修订代理指标和 rubric；**旧版本保留为回归集**；经 ask_human 告知你，但不等待 | 第二个 judge（不同模型）或你的打分与主 judge 不一致；分数涨但 artifact 明显变差（长度/格式刷分）；所有子代同分（无区分度）；LLM 坍缩 |
| 4 能力上限（worker 做不到） | 换更大 worker 或加工具（通过 ask_human 提议，因为涉及成本） | 同一 genome 换更大模型（如 72B / Claude）做一次探针评估，分数大幅上升 |
| 5 框架（genome 表达不了、搜索过程卡住、学习/评估机制本身有害） | 写一条框架改进提案，交给 §8 的框架层进化 | 见 §8.2 的信号 |

**动作的约束（防止系统给自己出题刷分）**
- 第 2/3 层改动都产生新的评估版本 `goal/v<N+1>/`（与 §4.4 同一机制），`CHANGELOG.md` 写明“由诊断触发、依据哪些证据”；
- **旧版本保留为回归集**：之后的接受条件 = 在新版上 `F_child > F_parent + margin`，且在每个旧版本上不比父代差超过 ε（默认 0.03）；
- **冠军必须在新版上掉分**：放大后的评估若不能让当前冠军掉分，说明没打到瓶颈，这次放大作废；
- 第 3 层不改你的 brief 原文，只改系统对它的理解（restatement、代理指标、rubric 权重），并经 ask_human 告知；你回答“不对”时按 §4.4 回滚或修订；
- 每次诊断写 `rationale/diagnosis_genXX.md`（证据包 + 判定层 + 动作 + 理由），并在 `design_log.md` 留一条，属于取舍记录的一部分。

**接口（实现时）**
- `Task.diagnostics(parent_eval, history) -> dict`：插件计算证据包（brief：分差、场景聚类、criteria 天花板、第二 judge 一致性；benchmark：失败原因分布、holdout 分差、LLM 角色数）；
- `Task.widen(layer, diagnosis) -> bool`：brief 模式由 Framer 的 `widen` 提示实现（2 层加场景/persona/措辞变体，3 层改代理指标/rubric）；
  benchmark 插件只有提供了数据构建器（如 `ood/build_v2.py`）时才执行 2 层，否则只记录诊断并 ask_human；
- `Evolution.maybe_diagnose()`：在触发条件满足时调用；判定为第 1 层且已连续诊断两次仍无进展时才真正停止。
- brief 模式默认开启；benchmark 模式默认只诊断、不自动改数据（与“不做 benchmark 自动目标升级”一致）。

## 8. 框架层进化（system-level evolution）：用更强的 Claude Code 会话改框架本身

### 8.1 为什么需要
genome 只决定团队在框架里怎么组织。**框架代码决定能组成哪些团队、怎么搜索、怎么学习、怎么评估**，这一层的瓶颈 genome 进化多少代都解决不了。
到目前为止，这一层一直是人在改，每次都是被观察到的失败逼出来的：

| 观察到的失败 | 瓶颈 | 人做的框架修改 |
|---|---|---|
| v1 每 agent 反思让 MATH 掉分 0.719 → 0.661 | 学习机制有害 | 门控学习（新笔记要在 holdout 上胜过旧笔记） |
| AppWorld 需要环境循环 | genome 表达不了 | 单独手写 runner 和 schema |
| calendar 被 regex 刷满分 | 评估太窄 | 难数据 + holdout |
| 72B judge 先打 0.97，校准后又对所有场景给同一个 0.475 | 评估无区分度 | 改 judge 提示、换模型 |
| 子代工具代码能读评估数据 | 隔离漏洞 | 静态检查 |
| 共享记忆和 `{human_answers}` 重复注入、只罗列不提炼 | 记忆机制粗糙 | （待做） |

§8 把“观察失败 → 改框架 → 验证 → 记录”这一圈机制化，执行者是一个比 evolver 更强、权限更大的 **Claude Code 会话**（能读写代码、能跑测试）。
和 DGM 的区别要说清：DGM 里任务技能（写代码）就是自我修改的技能，改进会递归叠加；我们的任务是写 PRD，团队变强不会让它更会改框架。
所以这里**不是让团队改自己**，而是在最外层加一个慢循环，让一个固定的强改进者按证据改框架，由固定的元评测把关。

三层循环：

| 循环 | 改什么 | 谁改 | 频率 | 把关 |
|---|---|---|---|---|
| 快 | `memory/<role>.md` | 角色自己（worker） | 每批经验 | holdout 门控 |
| 中 | genome | evolver（`claude -p`，只能读写工作区） | 每代 | fitness + margin |
| **慢** | **`src/sea/` 框架代码** | **框架进化者（Claude Code，git worktree，可跑测试）** | **每几次运行 / backlog 有高优先级提案时** | **固定元评测 + 受保护路径 + 你决定合并** |

### 8.2 信号 → 提案（`meta/backlog/P<NNN>.md`）
提案有四个来源，每条都要带证据（文件路径、计数、运行 id）：
1. **evolver 的愿望清单**（最便宜、信号最强）：evolver 指令里加一条：如果你想要的机制在 schema/框架里表达不了，写进 `FRAMEWORK_WISHES.md`（想要什么、为什么、你现在用了什么变通）。每代收集。
2. **自动检测器**（每代末尾，在 `sea.evolve` 里跑，纯程序）：
   - 无效 genome 聚类：同一类校验错误或同一种不支持的构造反复出现（≥3 次）；
   - judge 无区分度：一个子代所有场景同分，或一代所有子代的分数方差接近 0；
   - 学习门控拒绝率持续 100%（笔记机制无效）；
   - 搜索坍缩：子代结构签名相同的比例过高，或激进子代连续 N 代都不入档；
   - 成本异常：judge 或团队 token 突增、运行超时。
3. **§7 的诊断者判为第 5 层**。
4. **人**：你直接写一条，或者在 ask_human 里回答“这个应该是框架问题”。

提案格式：`问题 · 证据 · 影响（哪些运行、多大代价）· 建议方向（可选）· 类别（expressiveness | search | learning | evaluator | infra）· 验收标准`。
同类提案自动合并计数，按“出现次数 × 影响”排序。

### 8.3 框架进化者（执行）
- `python -m sea.meta.improve P007`：从当前提交切一个 **git worktree**（分支 `meta/P007`），在里面启动 headless Claude Code
  （`claude -p --model opus`，工具 Read/Edit/Write/Glob/Grep/Bash，设置轮数和时间上限），输入 = 提案 + 证据文件 + `docs/architecture_v2.md` + 受保护路径清单 + 元评测命令。
- 它必须：实现改动、补单元测试、跑 `pytest`、跑元评测冒烟、写 `meta/changes/P007.md`（改了什么、为什么、风险、怎么回滚）。
- 也可以不用 headless，而是起一个**可见的 Claude Code 会话**（桌面 app 里的一个 session）执行同一个提案，你能随时看和插话。适合大改动。
- 一次只合并一个提案；不同提案可以在不同 worktree 里并行开发，按顺序评测、合并。

### 8.4 受保护面（框架进化者不能改，机械检查 diff）
- **评估器**：`calevo/verifier.py`、`calevo/official_grader.py`、`calevo/tasks.py` 的打分、`data/splits/`、`sea/tasks/brief/judge.py`、`framer.py`、`sea/meta/bench/`（元评测本身）；
- **隔离规则**：`genome.check_tool_safety`、evolver 工作区的组装（`Task.workspace_files` 不得加入 holdout/test 内容）。
- 例外：类别为 `evaluator` 的提案（比如今天 judge 无区分度的问题）可以改评估器，但**必须由你批准**，并且必须通过一组**校准对**：已知好坏顺序的 artifact 对（例如“缺决策记录的 PRD < 有决策记录的 PRD”、
  人工打过分的设计），新评估器要排对至少同样多，而且场景之间要有区分度。
- diff 触碰受保护路径而提案类别不对 → 直接拒绝，不进入元评测。

### 8.5 元评测（固定，在可修改范围之外）
元评测的**评估器代码取自基线提交**，不用候选分支里的版本，这样候选改不松自己的打分器。分三档，逐档通过才往下跑：

| 档 | 内容 | 目的 | 粗估成本 |
|---|---|---|---|
| A 正确性 | 全部单测 + 新测试 | 不坏 | 秒级 |
| B 回归 | calendar dev_in@0:20（S0、P0）与基线逐条比对 prompt token；MATH dev@0:30 分数在噪声内；brief：固定的冻结团队 artifact 用基线 judge 重评，结果不变 | 不改坏已有行为 | ~10 分钟，<$1 |
| C 有效性 | 用候选框架跑短进化：1 个 brief（2 代、k=2）+ 1 个 benchmark（calendar，2 代），和基线框架同配置的运行比较“Δfitness / 美元”和提案自己的验收标准（例如“evolver 写出了用到新构造的有效 genome”） | 改进确实有用 | ~1 小时，$5-15 |

C 档噪声大（单次进化的方差），所以判定规则是：A、B 必须过；C 不显著变差，并且提案自己的验收标准达成。只做基础设施或表达力的提案，可以只要求 A、B 和验收标准。

### 8.6 合并与记录
- 通过元评测 → 合进 `evolve/main` 集成分支，**合进 `main` 由你决定**（默认）。
- 每次合并写一条 `docs/framework_log.md`：提案 id、证据、改动、元评测三档结果、谁批准。它对框架的作用，相当于 `design_log.md` 对设计。
- 每次运行在 `state.json` 里记下 `framework_commit`；不同框架版本的运行结果不直接比较，除非重跑。
- 失败的提案带着元评测结果回到 backlog（iterative，和 genome 进化同一个思路），最多重试 2 次，之后标记为需要人看。

### 8.7 第一批候选提案（来自今天的证据）
- P001 记忆：去掉 `_shared.md` 和 `{human_answers}` 的重复注入；把人类回答提炼成有上限的“团队品味”；评语进入共享记忆（类别 learning）。
- P002 搜索：从档案选父代（MAP-Elites 真正用于选择，而不只是记录）（类别 search）。
- P003 诊断：实现 §7（类别 search/evaluator，涉及评估器的部分需要你批准）。
- P004 评估：judge 区分度检测器 + 校准对（类别 evaluator）。

用 P001 做第一次真实运行最合适：范围小、受保护面清楚、验收标准明确（token 下降、行为不变）。

## 9. 预留接口（本轮只做最小实现或 stub）

- **多样性档案（MAP-Elites）**：`Selector` 接口有 `propose_parent()` 和 `add(child, eval)`。`MapElitesArchive` 描述子默认
  `(n_llm_roles, token 档, 机制标签)`，机制标签由 evolver 在 notes 里声明。本轮：每代都写入档案（`archive.json`）但仍用 greedy 选父代；切换只要换 selector。
- **每代一个激进子代**：`--radical 1` 时 evolver 指令要求 child_k 结构上不同于父代（换拓扑/角色类型/通信方式），
  它不必赢父代，只要进入档案的空格子就保留。本轮实现指令和标记，判定逻辑简单（结构签名不同 + F ≥ F_parent − 0.1 就入档）。
- **ask_human 非阻塞通道**：本轮完整实现（文件 inbox）；将来可以换成 Slack/Doc 后端，接口 `ask/poll` 不变。
- **新奇性奖励**：`FitnessSpec` 可以加一项 `novelty`，本轮不加。
- **benchmark 上的自动目标升级**：按你的要求**不做**（不自动造新 benchmark 目标）。brief 模式下的“目标放大”见 §7：只放大系统自建的代理评估，不改你的 brief，第 3 层改动会经 ask_human 告知你。

## 10. 演示计划（交付 2）

- brief：`briefs/day_planner.md` ——“帮忙安排一天的日程助手”，故意写得含糊（目标用户、平台、是否接日历都没说）。
- 起始团队 `brief_P0`：单个 PM 角色，直接写 PRD + decisions。
- 跑 2-3 代、k=2-3 子代，worker = qwen3-30b-a3b（:8101，小规模），Judge = 同一 worker 或 qwen2.5-72b（:8102），
  Framer = Claude（每个评估版本只调一次），evolver = Claude opus（同 v1）。
- 演示中我会手动往 `answers.jsonl` 写 1 条回答，展示评估改版 → 父代重评 → 取舍复查这条链路。
- 预期产出：`goal/v1,v2`、`inbox/`、每代 artifacts、`rationale/genXX.md`、`design_log.md`。

## 11. 回归检查（交付 3）

1. `src/calevo/verifier_test.py` 不动、继续通过；新增 `src/sea/*_test.py`（genome 适配、executor 用假 LLM 后端、inbox、门控逻辑、fitness）。
2. `python -m sea.run --task bench:calendar --genome genomes/S0 --split dev_in --limit 10`，与 `calevo.runner` 同条件结果逐样本比对
   （temperature 0，S0 主要是代码路径，应完全一致或仅有 vLLM 批次非确定性差异）。

## 12. 需要你拍板的问题（不等你：括号里是我的默认）

1. **Judge 用什么模型？** 本地 qwen 便宜但打分粗；Claude 准但每次 30s+ 且花钱。（默认：Framer 用 Claude，Judge 用本地 qwen2.5-72b，可一键切 Claude）
2. **复杂度是惩罚项还是 rubric 一条？** 惩罚项会像 token 惩罚那样把功能“进化没”。（默认：惩罚项，λ_cx 小，且 rubric 里有“MVP 克制”）
3. **团队每次评估跑一次还是多次？** 多次可估方差但贵 R 倍。（默认：1 次，靠 accept margin 抗噪）
4. **你的反馈形式**：只回答问题，还是也给每代打分/写评语？（默认：两者都支持，打分是可选的 fitness 项）
5. **文档语言**：仓库现有文档是英文，这份是中文给你审。（默认：确认后保留中文版并在 README 用英文概述）
6. **包名** `sea`（新）+ `calevo`（保留为 benchmark 库）可以吗？（默认：可以）
