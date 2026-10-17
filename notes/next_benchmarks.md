# Next benchmark candidates (subagent survey, 2026-10-17 ~14:40 UTC)

Life-assistant (A):
1. AppWorld (StonyBrookNLP/appworld, Apache-2.0, test bundle encrypted): 750 tasks, 9 apps / 457 APIs, offline DB-state unit-test grading, no judge. GPT-4o ~49% normal / ~30% challenge. Open-ended API programming -> no closed-form solver. Supports vLLM. TOP PICK (A).
2. tau2-bench (sierra-research, MIT): airline 50 / retail 115 / telecom 114; DB hash x communicate check; needs an LLM user simulator (fix model+temperature, pass^k). gpt-4.1 pass^1 74/56/34%. Also shipped in NeMo Gym.
- Not recommended: TravelPlanner (LLM+SMT already 93.9% -> solver collapse again), NATURAL PLAN (closed-grammar), NeMo Gym Workplace Assistant (templated tool-call matching).

RSI / agent-design comparables (B):
| paper | benchmarks | executor |
|---|---|---|
| ADAS | ARC subset, DROP, MGSM, MMLU, GPQA; GSM8K/GSM-Hard transfer | GPT-3.5 |
| AFlow | HotpotQA, DROP, HumanEval, MBPP, GSM8K, MATH Lv5 (617) | gpt-4o-mini |
| MaAS | GSM8K, MATH, MultiArith, HumanEval, MBPP, GAIA | gpt-4o-mini (+Qwen2.5-72B) |
| GEPA | HotpotQA, IFBench, HoVer, PUPA, AIME-2025, LiveBench-Math | Qwen3-8B, GPT-4.1-mini |
| MASS | MATH, DROP, HotpotQA, MuSiQue, 2Wiki, MBPP, HumanEval, LiveCodeBench | Gemini 1.5 |
| DGM | SWE-bench Verified staged subsets, Polyglot | Claude 3.5 Sonnet (~2 weeks/run) |
AFlow vs ADAS (gpt-4o-mini): MATH 56.2/35.4, MBPP 83.4/53.4, DROP 80.6/76.6, HotpotQA 73.5/64.5.
TOP PICK (B): MATH Lv5 (AFlow 617 split) + DROP — offline grading, widely reported; compare gains over IO/CoT baselines (different executor), GEPA gives a Qwen3-8B reference.
Unverified: AppWorld per-split counts; tau2 with local vLLM; Qwen3-30B scores on these.
