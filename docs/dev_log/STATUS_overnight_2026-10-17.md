# Calendar-Evo status (local mirror of the target doc)

Target doc: https://claude.ai/code/artifact/36d2a598-fabd-408b-806e-ee9a864ae4be

## Targets
| # | 工作流 | 状态 |
|---|---|---|
| W1 | Verifier + runner | 完成：官方 grader 封装，4 个单元测试通过；test_in 128/128 可行 |
| W2 | Baseline | 完成（8B/30B/72B × P0/S0）；ood_test/test_in 的 30B baseline 运行中 |
| W3 | OOD 偏好集 | 完成并抽查：ood_dev 50（已冻结；11 处 at-事件的时间窗偏好必然满足，A 信号略被稀释）、ood_test 60（按抽查修复后重建） |
| W4 | Genome + 进化器 | 完成（P0/S0 genome，evolve.py） |
| W5 | 进化运行 | evo_full、evo_full_8b、v2guard 已完成；evo_prompt gen6 0.523；lam005 gen5 0.9606（保留 LLM） |
| W6 | 报告 | evo_full 的 3 个测试集已出分（见中期结果）；还差 evo_prompt 和消融 run |

## 中期结果（12:22 UTC，30B worker，测试集）
| 团队 | test_in | ood_test（未见偏好） | test_v2（长对话） |
|---|---|---|---|
| P0 单 prompt | 0.320（4141 tok） | 0.150 | 0.359 |
| S0 固定 DSL 求解器 | 0.414（4294） | 0.121 | 0.418 |
| **evo_full 进化 10 代（干净）** | **1.000**（907） | 0.625 | **0.824** |
| evo_full_leaky（见过 B 类偏好名，仅作参考） | 0.984（2981） | 0.925 | 0.723 |
| 8B 0 token regex（evo_full_8b） | 1.000（0） | 0.575 | 0.410 |
| 8B 0 token regex + dev_v2（v2guard；test_v2 对它算同分布） | 0.984（0） | 0.583 | 0.981 |
| 8B + dev_v2，λ=0.005（保留 LLM；test_v2 同分布） | 1.000（2566） | 0.629 | 0.953 |

ood_test 拆开看：evo_full 硬约束 1.00，偏好满足 right_after 12/48、order 4/12、keep_free 1/5；leaky 是 44/47、8/12、5/5。
结论：进化团队在硬约束和长对话上泛化好；没见过的偏好类型基本处理不了，差距完全来自"知不知道这种偏好存在"。注意 ood_test 里 keep_free 只有 5 条，类型分布不均。
下一步方向：在 fitness 里放"从未出现过的偏好"的代理信号，例如每代留出一种偏好类型做 held-out。

## Baselines (score; dev_in / ood_dev; tokens/sample)
| genome | worker | dev_in | ood_dev | tokens |
|---|---|---|---|---|
| P0 | qwen3-8b | 0.42 | 0.13 | 2744 |
| P0 | qwen3-30b-a3b | 0.36 | 0.095 | 2745 |
| S0 | qwen3-8b | 0.25 | 0.19 | 2898 |
| S0 | qwen3-30b-a3b | 0.32 | 0.275 | 2881 |
| P0 | qwen2.5-72b | 0.48 | 0.165 | 2761 |
| S0 | qwen2.5-72b | 0.64 | 0.26 | 2923 |

## 进度日志 (UTC, newest first)
| time | ws | note |
|---|---|---|
| 17:31 | 每 agent 学习 | 用户同意先在 MATH 上做每个 agent 的记忆。runner 读 genome/memory/<role>.md 注入到该角色的 prompt；calevo.learn：团队在 math_train（MATH train L5，400 题，和 dev / test 不相交）上做一批题，每个 LLM 角色用 worker 模型自己反思自己的输出，重写笔记（最多 12 条、900 字、只写通用经验）。E1（结构固定、只学习，6 轮 × 40 题，每轮评 math_dev）：learn_math_P0（单 agent）和 learn_math_team（evo_math 最终 6 人团队）。E2：evo_math_learn = evo_math 加上 CALEVO_LEARN=math_train:40，子代继承父代同名角色的笔记、评估前先学一轮（拉马克式），对照组是 evo_math |
| 17:25 | 阶段 3 | evo_math 结束（最好 gen9，dev 0.851，6 人 / 3 LLM），evo_drop 结束（dev 约 0.92，3 人 / 2 LLM），evo_full_diverse 结束（fit 0.959）。测试集评估已启动：math_test 484 题（最终 genome + P0，8B），drop_test 800 题（最终 genome + P0，30B），diverse 跑 3 个测试集。evo_hard gen9 fit 0.994；evo_appworld 正在评估 gen00（72B，train 90 题） |
| 17:15 | AppWorld | genome 评测器完成（appworld/run_genome.py）。P0 用 30B：dev TGC 7.0%（基线 5.3%），train TGC 17.8%、平均通过比例 0.482，SGC 6.7%。30B 服务器太挤（running 66 / waiting 84，会超时），所以 evo_appworld 改用 qwen2.5-72b，从 P0 出发，fitness 是 train 平均通过比例 − 0.0005·k-token。tool 代码加了静态检查：引用 appworld 内部、文件或 ground truth 的 genome 一律拒收（负例已验证） |
| 17:03 | 发布 | 按用户要求整理代码，推到私有仓库 https://github.com/beckybyte/self-evolving-agents（新历史，单个提交 c422c12，106 个文件，含 data/splits）。导出时清掉了本机路径和公司内部镜像地址；AppWorld 加密数据没有上传，也已在本地取消跟踪 |
| 17:03 | 发布 | 按用户要求整理代码，推到私有仓库 https://github.com/beckybyte/self-evolving-agents（新历史，单个提交 c422c12，106 个文件，含 data/splits）。导出时清掉了本机路径和公司内部镜像地址；AppWorld 加密数据没有上传，也已在本地取消跟踪 |
| 16:56 | — | 全部健康。math gen9 fit 0.814（dev 0.851，6 人 / 3 LLM），drop gen9 fit 0.904，hard gen4 fit 0.992，diverse gen9 第 3 次尝试。AppWorld P0 在 train 上做验证（子 agent） |
| 16:29 | 阶段 2/3 | evo_hard gen2 fit 0.991：dev_hard 很快就近乎解决了，关键看留出类型 test_hard_h。evo_prompt 测试集 0.672 / 0.438 / 0.555，每样本约 11k tok；对照 evo_full 1.000 / 0.625 / 0.824，907 tok，纯 LLM 团队贵 12 倍还更差。math gen8 0.798，drop 0.903（gen7 第 2 次尝试），diverse 0.959。AppWorld genomes/P0 已就绪，子 agent 在做验证 |
| 16:06 | 阶段 2 | hard baseline（score，dev_hard / test_hard_v / test_hard_h）：P0 30B 0.08/0.15/0.11；S0 0.11/0.18/0.15；v1 最佳 30B 0.79/0.65/0.41（test_hard_h 上连硬约束通过率都只有 0.55：自由措辞把 regex 解析弄坏了）；λ=0.005 8B 0.59/0.57/0.59。余量充足，从 v1 最佳 genome 出发启动 evo_hard（0.3·dev_in + 0.7·dev_hard，λ=0.01）。math gen6 0.767，drop gen7 0.903，diverse 0.9575 |
| 15:35 | 阶段 2/3 | data v2 全部生成：dev_hard 80 / test_hard_v 60 / test_hard_h 80，各约一半带冲突，back-check 合计只丢 1 条。启动 hard baseline（P0、S0、v1 最佳用 30B，λ=0.005 genome 用 8B，3 个 split）。AppWorld 已装好（dev 57 / test_normal 168 / test_challenge 417），子 agent 在跑 ReAct baseline。进化：math gen4 0.746，drop gen6 0.893，diverse gen7 0.9575 |
| 15:08 | 阶段 2/3 | evo_math（8B）gen2：dev 0.678 → 0.769，团队长到 5 个角色（solver / checker / exec / compare / judge），是第一次出现多于 3 个角色，而不是收缩成代码。evo_drop（30B）gen4：0.797 → 0.895，3 个角色（assistant / checker / normalize）。evo_full_diverse gen5 fit 0.932。evo_prompt 结束（0.523），测试集评估在跑。test_hard_v 已生成 60 条，test_hard_h 生成中 |
| 14:47 | 阶段 3 | P0 dev 基线：MATH Lv5 30B 0.694（1319 tok，31/121 没有 \\boxed）/ 8B 0.703；DROP 30B 0.802 / 8B 0.790。修了 DROP 金标签：多个 span 是不同标注者的候选答案，不是一个多 span 答案，修复前分数被压到 0.51。启动 evo_math（8B，可和 GEPA 的 Qwen3-8B 对照）和 evo_drop（30B），λ=0.01 |
| 14:22 | 快照 | 用户判断：模型迭代已到顶，下一步要迭代数据。存档到 snapshots/v1_model_top/（所有最佳 genome 含工具代码、各 run 的 history、测试集 report、REPORT.md、曲线、SNAPSHOT.md），git tag v1-model-top。员工数：evo_full 1→2→3 后稳定（只有 1 个 LLM 角色，其余是代码）；evo_prompt 1→2；8B 在 λ=0.02 下变成 0 个 LLM；λ=0.005 停在 3 个角色、1 个 LLM。4 个角色的方案只被提出过 1 次，从没胜出 |
| 14:12 | W5 W6 | λ=0.005 结束（fit 0.9836，brief(code) + assistant(LLM) + repair(code)）。测试集 1.000 / 0.629 / 0.953。和 λ=0.02 的 regex（0.583）相比，未见偏好略好：order 8/12 对 5/11，但 right_after 仍只有 10/47，因为 repair 代码在后处理时把不认识的偏好信息丢了。所有 Target 已达成，按指示继续改进。新实验 evo_full_diverse（30B，λ=0.02，与 evo_full 只差一个变量）：fitness 加入 C 类偏好集 ood_dev_c（avoid_window / min_gap，50 条，与 A、B 都不相交，与 dev_in / ood_dev 无重叠），进化器能看到 A + C 两类，仍在 B 上测。假设：见过的偏好种类变多，会逼出通用的偏好解释机制 |
| 13:40 | W5 | λ=0.005 到 gen9：fit 0.9795（0.99 / 0.99），1845 tok，仍保留 LLM（brief + assistant + repair），gen10 跑完就结束。evo_prompt stale 1。进化器累计调用 77 次（约 $37） |
| 13:13 | W6 | v2guard 测试集：test_in 0.984 / ood_test 0.583 / test_v2 0.981。dev_v2 让 regex 也学会了 v2 的措辞（test_v2 从 0.41 升到 0.98），但 ood_test 没变：纯代码只对见过的措辞泛化，没见过的偏好类型照样不行。λ=0.005 到 gen7 fit 0.974，仍保留 LLM；evo_prompt 停在 0.523 |
| 12:48 | W5 W6 | v2guard 结束，最终仍是 0 token regex（fit 0.989），测试集评估中。λ=0.005 到 gen5：0.99 / 0.965，2744 tok，仍保留 LLM（assistant + repair）。中期结果已同步到文档 |
| 12:22 | W6 | evo_full 最终测试集：test_in 1.000、ood_test 0.625（硬约束 1.00，未见偏好大多没满足）、test_v2 0.824，约 900 tok。leaky 版 ood_test 0.925，说明差距来自偏好类型名的泄漏。其余 run：evo_prompt 0.523、v2guard 0.989（0 tok）、lam005 gen3 0.9506。doc 工具超时，下轮同步"中期结果"小节 |
| 12:01 | W5 W6 | evo_full 跑满 10 代：最终 genome gen10_a1_child_1 = digest(code) + extract(LLM, 768 max tok, 位置行格式) + answer(code, solve.py)，代码约 1000 行；fit 0.9837，708 tok/样本，是 P0 的 26%。开始测试集评估。λ=0.005 消融的第一个被接受 genome 保留了 LLM（assistant + repair，0.91 / 0.79）；在 λ=0.02 时同类方案会输给 0 token 的 regex。8B S0 测试集 baseline：0.141 / 0.037 / 0.234 |
| 11:34 | W5 | evo_full gen9 又有提升：fit 0.9825，745 tok，gen10 跑完就结束。v2guard 0 token 的 regex 到 fit 0.96。lam005 第一批子代全部比 P0 差，正在重试。evo_prompt 还活着，只是慢（多角色、每样本约 10k tok，和其它 run 共用 30B 服务）。8B baseline 测试集：P0 test_in 0.242 / ood_test 0.092 / test_v2 0.383。（更正：上一行实际写于 11:13） |
| 11:20 | W5 消融 | v2guard 的结论：把 dev_v2 放进 fitness 也挡不住退化成 regex。gen01_a2 里带 LLM 的子代 dev_v2 0.79 / 2782 tok，纯代码子代 0.74 / 0 tok，后者赢在 token 惩罚（λ·tok ≈ 0.06 大过准确率差）。所以 λ 才是关键旋钮。启动 evo_full_8b_v2guard_lam005（λ=0.005，split 相同），看保留 LLM 的方案能否胜出并泛化得更好。evolve.py 的 λ 改成可配置（CALEVO_LAMBDA，默认 0.02 不变）。evo_full_8b 已结束 |
| 10:45 | W5 | 四个 run 都健康。evo_full stale 1（gen7 三次尝试最高 0.975，没超过 0.981），快结束了。evo_full_8b 已到 fitness 上限 1.0，只会靠 patience 停下。v2guard 第一批子代全部比 P0 差（8B extractor 在 dev_in 上只有 0.15–0.27），正在重试。（更正：上一条日志时间应为 10:29） |
| 10:29 | W5 | 关键发现：evo_full_8b 到 gen6 时把 LLM 完全去掉了，变成单个 tool 角色、约 1000 行 regex 解析 + 求解器，**0 token**，dev 两集都满分。审计：没有读文件，是合法解法。抽查测试集：test_in 1.00；ood_test 0.575（硬约束 0.97，偏好只有 0.19，没见过的偏好类型几乎全忽略）；test_v2 0.41（措辞一变就崩，低于 S0 的 0.42）。这正是"泛用 solver 碰到用户偏好不 work"的实证：token 惩罚加上太窄的 dev，把进化推成了过拟合的 regex。消融：evolve.py 的 fitness split 改为可配置（默认值验证不变），用 v2 train 100 条（与 v2 val 无重叠）作 dev_v2，启动 evo_full_8b_v2guard（0.4·dev_in + 0.3·ood_dev + 0.3·dev_v2）。注意：这个 run 里 test_v2 算同分布，泛化只看 ood_test |
| 10:27 | W5 | evo_prompt 首次突破：gen4 fit 0.451（0.74 / 0.43，7.5k tok） |
| 10:00 | W5 | evo_full gen5：dev 两个集都满分，842 tok/样本（P0 是 2743）；之后的代际只在压 token。风险：dev 已饱和，进化器开始针对单个失败样本写 regex 补丁（如 oodA-2217），可能过拟合，要看 test_v2 和 ood_test。evo_prompt 的纯 prompt 团队越长越贵（最多 12k tok），分数不升。8B：gen2 0.88 / 0.755 |
| 09:40 | W5 | 干净的 evo_full 到 gen3：fit 0.958，tokens 降到 P0 的 47%（digest + extract + answer）。leaky 参考版在测试集上：test_in 0.98、ood_test 0.925、test_v2 0.72（长对话里数错事件数 46 次）。P0/S0 的 test_v2 分别为 0.36 / 0.42。启动 evo_full_8b，看这套流程能否换到 8B worker 上 |
| 09:30 | W3 | 子 agent 抽查 25 条：0 条歧义、0 条坏样本；系统性问题是名字截断、裸 id、首字母小写。ood_test 尚未被进化用过，修复 build.py 后重建并重跑其 baseline；ood_dev 已冻结，保持不动 |
| 09:20 | W2/W5/W6 | 30B 在测试集上的 baseline：P0 test_in 0.32 / ood_test 0.146；S0 0.414 / 0.133。干净的 evo_full gen1 0.85 / 0.725（extract + answer 两个角色）；evo_prompt gen2 第 1 次尝试无提升，正在第 2 次尝试。report.py 写好；leaky 版在测试集上的评估作为参考在跑；子 agent 在抽查 OOD 集 |
| 08:56 | W5 | LEAK FOUND & FIXED: evolver tool doc listed B-type pref names (keep_free/right_after/order) reserved for ood_test. evo_full archived as evo_full_leaky (gen2: dev_in 0.97, ood_dev 0.96, 2341 tok, roles compact+extractor+solver) and restarted clean. evo_prompt never saw the tool doc -> unaffected |
| 08:52 | W3 | ood_test only 6 rows: days are ~full (345-360 of 360 min) so 1h keep_free was unsatisfiable. Switched to 30-min windows + random 1-2 pref combos -> 60 rows |
| 08:50 | W2 | 72B: P0 0.48/0.165, S0 0.64/0.26 |
| 08:20 | W5 | evo_full + evo_prompt launched on 30B worker |
| 08:18 | W2 | 8B/30B baselines done (table above); main failures: overlaps, extraction errors |
| 08:15 | research | no framework to adopt; borrow ADAS evolver prompt + GEPA feedback. v2 val added as test_v2 (long-convo OOD) |
| 08:10 | W1/W3 | verifier wraps official grader; ood_dev built |
| 07:56 | — | env check; conda vLLM broken -> docker vLLM image |

## 待用户决定
- 下一轮实验是否把 dev_in 从 100 条扩到约 300 条？evo_full 在 dev 上已饱和，fitness 只剩 token 信号。本轮为了可比性不改。
- 09:40 我的 shell 失误：一次 `git commit` 落进了 **<another repo> 的 GFSQ 分支**（commit 9d8a3b8，"tick 09:40: evo_full gen3, launch evo_full_8b"），把你原本 untracked 的 4 个文件提交了：.quota_probe、OVERNIGHT_PROMPT_decoder_gan_ft.md、docs/for-claude/tts_leaderboard_issue_drafts.md、tools/visqol.py。没有 push，文件内容都没变。我想用 `git reset --mixed HEAD~1` 撤销，让它们恢复 untracked，但这一步被权限拦了。请你自己执行：`git -C <local_path> reset --mixed HEAD~1`。
