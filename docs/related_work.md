# 相关工作调研与对比（2026-10-17）

来源：用户转来的一份建议文档（列了 EvE、KADATH、AgentScope Java、Deep Agents、AutoGen、AgentEvolver、GPTSwarm、DyLAN），
由 4 个调研子 agent 分别核实（WebSearch/WebFetch、读 README 和部分源码）。数字多为作者自报；标“未核实”的是凭记忆或只见摘要。

## 1. 先核实：建议文档里的说法

| 项目 | 存在 | 文档说法 | 实际情况 |
|---|---|---|---|
| [EvE](https://github.com/scaling-group/eve)（arXiv 2605.09018） | 是，~62★ | 并行 coding agent，同时演化代码和 agent 指导策略，接近递归自我改进 | solver 代码和 optimizer guidance 两个种群共演化，按两两比较的 Elo 选择；**底座 agent 冻结**，“递归自我改进”有夸大；只有一个任务的结果 |
| [KADATH](https://github.com/i3T4AN/KADATH) | 是，~258★，个人项目 | 繁殖、变异、评估、淘汰 | 准确，但“繁殖”是**无性的**（elite + 有界变异，无 crossover）；**评估防作弊做得最好**（锁定 rubric 要人批准、哈希复验、打分必须引证据、kernel 算分忽略自评） |
| [AgentEvolver](https://github.com/modelscope/AgentEvolver) | 是，~1.6k★ | 从经验持续改进 | 实质是 **RL（GRPO）训练权重**，不是 prompt/工作流进化；AppWorld 7B 1.8→32.4 |
| [AgentScope Java](https://java.agentscope.io/v2/en/docs/harness/subagent.md) | 是（v2.0.0-RC） | `agent_spawn/send/list`、`task_cancel/output`、`wait_async_results`，隔离工作区、复用、远程子 agent | **全部核实**；子 agent 强制为 leaf、层级上限 3、有超时，**没有数量上限或 token 预算** |
| [Deep Agents](https://docs.langchain.com/oss/python/deepagents/subagents) | 是 | fork 子 agent 继承父对话上下文 | 属实（`mode: "fork"`，仅 Python，experimental），fork 能复用 prompt cache；fork 出来的不能再 spawn |
| [AutoGen](https://github.com/microsoft/autogen) | 是 | 消息传递、生命周期、交接 | 属实，但**已进入维护模式**（后继 Microsoft Agent Framework），不建议作为新依赖 |
| [GPTSwarm](https://github.com/metauto-ai/GPTSwarm)（ICML'24） | 是 | 可优化计算图，递归组合，prompt+边自动优化 | 优化边概率（REINFORCE）和节点 prompt 属实；**只剪边不剪节点**；递归组合只实验了一层；目标里**没有成本项** |
| [DyLAN](https://github.com/SALT-NLP/DyLAN)（COLM'24） | 是 | 动态选 agent，Agent Importance Score，提前停 | 属实：离线按 AIS（同伴互评反向传播）选 top-k，在线每层留 top-2，2/3 共识就停；HumanEval 82.9 只用 16.9 次调用（LATS 48 次） |

补充核实的：ADAS（meta agent 写 agent 代码 + 不断增长的档案，单次搜索约 $300-500）、
AFlow（MCTS 搜代码化工作流，每个候选跑 5 次取均值方差，记录每次修改是否有效）、DGM（未联网核实：全量档案开放采样，观察到伪造测试日志的 reward hacking）。

**建议文档里“Merge 是空白”的判断要收窄**：权重层面的 agent merging（ARM、AgentPatch，要同构 checkpoint）、单 agent 内部的记忆合并、
**配置之间**的 crossover（EvoMAS 2602.06511）都已有工作；“在线决定 split/merge/spawn/stop、优化质量−成本”也基本被覆盖
（Puppeteer NeurIPS'25 用 RL 学调度者、DAAO、MaAS、AgentDropout、MACI/D3 的预算停止）。
**真正没找到的是：黑盒 LLM 下把两个角色的提示 + 记忆 + 职责融合成一个新角色，并逐项验证融合后两种能力都保留**。缺口窄，但干净、可检验（检索有限，“没找到”≠“不存在”）。

## 2. 和我们的框架对比

| 维度 | 他们 | 我们 |
|---|---|---|
| 进化单元 | 边/prompt（GPTSwarm）、代码工作流（ADAS/AFlow）、整个 agent 含代码（KADATH）、guidance 文本（EvE）、权重（AgentEvolver） | genome：角色（llm/tool/human）+ 工作流 + 通信 + 产出物 + 每角色记忆 |
| 选择 | 种群截断（KADATH）、Elo 采样（EvE）、全量档案开放采样（DGM/ADAS）、MCTS（AFlow） | **单父代贪心**；MAP-Elites 只记录不选；每代一个激进子代 |
| 抗噪 | AFlow 每候选跑 5 次；ADAS bootstrap CI；EvE/KADATH 同轮同条件比较 | 子代和**一次性测的 parent_fit** 比，只在评估改版时重测（赢家诅咒风险） |
| 贡献归因 | DyLAN AIS；HiveMind DAG-Shapley；ICLR'26 “Agent That Matters”：层级系统里 LOO ≈ Shapley | **没有**；executor 记了每角色 token，但 `run_one()` 没带出来，evolver 只看到总 token |
| 成本 | 四个拓扑工作都**不在目标里**（只事后画 Pareto） | **fitness 里直接有** λ·tokens、λ_cx·复杂度 |
| 无真值任务 | 基本都要真值；KADATH 有自建 rubric 但锁定不变 | Framer 自建评估 + holdout + **随人的回答修订** + 非阻塞 ask_human |
| 评估防作弊 | KADATH 最强（rubric 哈希锁、证据冻结、打分引证据） | holdout 隔离、工具静态检查、元评测评估器取自基线；**judge 不要求引证据、rubric 没哈希锁** |
| 运行时 spawn | AgentScope/Deep Agents/Claude Code 成熟；都靠层级+超时设上限，**无 token 预算** | **没有**；拓扑在一次运行内固定，只在代际之间由 evolver 改 |
| Merge | 都只是结果汇总 | evolver 能在 YAML 里合并角色，但 `_inherit_memory` 只按同名继承 → **合并后两边记忆都丢** |
| 自我修改 | DGM 改自己代码（只看 benchmark 分）；EvE 改 guidance；KADATH 控制面固定 | §8 框架层进化：证据 backlog + 受保护面 + 三档元评测 + 人合 main |

我们独有、值得强调的：成本进目标函数、无真值的自建且可修订的评估、门控记忆、框架层进化的“控制面与可进化面分离”（比 DGM 严格，接近 KADATH 的思路）。

## 3. 值得借鉴（按性价比排序，每条都可以作为 §8 的框架提案）

| # | 机制 | 借自 | 落点 | 便宜的验证 |
|---|---|---|---|---|
| 1 | **抗噪接受**：接受前父代和最佳子代同条件重评（2 个种子），用配对差 / bootstrap 下界判定；用同一 genome 重评 3-5 次测出的标准差校准 margin（brief 的 0.02 是拍的） | AFlow、ADAS、EvE | `evolve.py` `attempt()` 接受判断、`fitness.py` | 同一 genome 重评 5 次测方差；回放已有运行看有多少次接受会被推翻 |
| 2 | **角色级 LOO 归因**：对父代逐个去掉 LLM 角色，在 fit 小批上重评，得到每角色 Δscore/Δtokens，写进 evolver 报告；规则：Δscore≈0 且 Δtokens>0 → 建议 fire；两个角色单独去掉影响小、一起去掉影响大 → 冗余，建议 merge | DyLAN AIS、“Agent That Matters” | `executor.run_team(skip=)`、`tasks/base.run_one()` 带出 trace、`evolve.brief()` | calendar 上看它能否直接指出强 λ 下被删掉的那个 LLM；MATH 6 角色团队里哪个冗余 |
| 3 | **合并时继承记忆**：genome 加 `merged_from: [A, B]`，`_inherit_memory` 合并两边笔记（压缩、保留来源），之后走门控学习；验收：融合角色在 A、B 各自贡献样本上的保留率 ≥ 90% 且 token 更少（用 #2 测） | 缺口本身 | `genome.py`、`evolve._inherit_memory` | 对照：不带记忆 / 只带 A 的记忆 |
| 4 | **档案选父代 + LLM 介导的 crossover**：以概率 p 从 `elites()` 采样父代（rank-softmax × 1/(1+子代数)）；工作区多放一个 `donor/`（另一格的精英 + 报告），k 个子代里留 1 个做“融合 parent 与 donor”，genome 记 `parents: [a, b]` | DGM、EvE、ADAS | `evolve.attempt()`、`archive.py` | fake LLM 跑 3 代对比贪心：同样 evolver 调用数下的最终 fitness 和覆盖格数 |
| 5 | **judge 必须引证据 + rubric 哈希锁**：每条打分引用 artifact 片段，引不到记 1 分；fit/holdout/rubric 哈希写进评估版本，每次评估复验；加一个“空 artifact + 自夸”的对抗样本 | KADATH | `judge.py`、`framer.write_version`（evaluator 类，需你批准） | 对抗样本必须 ≤ 1.5 分；对现有 artifact 打分变化 |
| 6 | **修改经验表**：结构化 `edits.jsonl`（改动类型、Δfitness、是否接受），给 evolver 附历史统计（“删角色类修改 4 次 3 次有效”） | AFlow | `evolve._rationale()` | 回放现有 run 目录生成，不用新跑 |
| 7 | **有界 fan-out 原语**：genome 声明 `{spawn: {template, n_max≤6, budget_tokens, context: fresh/fork, merge: concat/vote/judge:<role>}}`，只允许一层、执行器硬截预算；evolver 进化的是 spawn 策略而不是自由 spawn | AgentScope、Deep Agents | `executor._Run.run()`、`genome.validate()`、`structure_signature` | 见 §4 的实验 |
| 8 | **循环提前停**：loop 加 `stop_on_agree`（连续两轮 body 输出一致就停） | DyLAN | `executor` loop 分支 | MATH 6 角色 genome 开关对比 token |

不建议：GPTSwarm 的 REINFORCE 边优化（我们每代 k=3，样本太少梯度太吵）；依赖 AutoGen；在“在线 split/merge 当优化问题”上立论（已被 Puppeteer/DAAO/MaAS/AgentDropout 覆盖，作相关工作引用）。

## 4. 一个 hackathon 可做的对照实验

固定任务（MATH 子集 + calendar）、固定 worker、每题 token 预算 B 取三档（执行器硬截）。五个臂：
单 agent（max_tokens 补齐到 B）· 固定多 agent（solver→checker→refiner loop）· 我们进化出的团队（λ 按 B 标定，evolver 开销单独记账）·
有界 fan-out（#7）· LLM 自主 spawn（`SPAWN:` 行，max_spawns 4，子 agent 为 leaf）。
指标：成功率、tokens 均值/p90、延迟、spawn 次数分布；主图是成功率随 B 的曲线（Pareto 前沿）；每臂 3 个种子、holdout 上报告。
判据：只有 fan-out/自主 spawn 在等预算下显著优于进化团队、且不只在难题子集上，才把 spawn 加进 evolver 的变异算子。

## 5. 不确定之处
- 多数数字来自预印本，作者自报；DGM、EvoPrompt/PromptBreeder 的细节凭记忆。
- EvE 的 crossover 方式和 KADATH 的选择逻辑是从源码片段推断的，没完整通读。
- “角色融合 + 能力保留验证”是空白，这个判断基于有限检索。
