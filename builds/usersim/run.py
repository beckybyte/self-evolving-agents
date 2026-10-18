"""CLI: python builds/usersim/run.py <app_dir> --split fit|holdout --episodes N --model ... [--out x.json]

Starts the app on free ports (one instance per parallel worker; --data in a temp dir), runs N simulated-user episodes,
prints per-episode invariant pass rate, violations with examples, coverage and the mean, and writes JSON to --out.
fit = seeds 0..N-1 round-robin over the 4 doc personas; holdout = seeds from the clock + generated personas.
"""
import argparse
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import personas as P  # noqa: E402
import sim  # noqa: E402

# assumed Crusoe list prices, USD per million tokens (input, output); used only for the cost estimate
PRICES = {"openai/gpt-oss-120b": (0.15, 0.60), "nvidia/Nemotron-3.5-Lightning-30B-A3B": (0.10, 0.40),
          "nvidia/NVIDIA-Nemotron-3.5-Super-VL": (0.30, 1.20)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self, app_dir: str, mock: bool = False):
        self.app_dir = str(Path(app_dir).resolve())
        self.port = free_port()
        self.data = tempfile.mkdtemp(prefix="usersim_")
        from sea import llm
        env = {**os.environ, "CRUSOE_API": llm.crusoe_key(),
               "CRUSOE_BASE_URL": os.environ.get("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1"),
               "MODEL_FAST": os.environ.get("MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"),
               "MODEL_SMART": os.environ.get("MODEL_SMART", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        if mock:
            env["EVOLVE_MOCK"] = "1"
        self.log = open(os.path.join(self.data, "app.log"), "w")
        self.proc = subprocess.Popen([sys.executable, "app.py", "--port", str(self.port), "--data", self.data],
                                     cwd=self.app_dir, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.base = f"http://127.0.0.1:{self.port}"
        t0 = time.time()
        while time.time() - t0 < 30:
            r = sim.http(self.base, "GET", "/health", timeout=3)
            if r.get("status") == 200:
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"app at {app_dir} did not answer /health")

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                pass
        self.log.close()
        shutil.rmtree(self.data, ignore_errors=True)


def cost(usage: dict, model: str) -> float:
    pin, pout = PRICES.get(model.split(":", 1)[-1].split("#")[0], (0.3, 1.2))
    return (usage.get("prompt", 0) * pin + usage.get("completion", 0) * pout) / 1e6


def plan_episodes(split: str, n: int, seed_base: int | None, gen_model: str) -> tuple[list, int | None]:
    if split == "fit":
        return [(s, P.DOC_PERSONAS[s % 4]) for s in range(n)], None
    base = seed_base if seed_base is not None else int(time.time()) % 1_000_000
    pool = P.generate(4, base, model=gen_model)
    import random
    return [(base + i, random.Random(base + i).choice(pool)) for i in range(n)], base


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("app_dir")
    ap.add_argument("--split", choices=["fit", "holdout"], default="fit")
    ap.add_argument("--episodes", type=int, default=4)
    ap.add_argument("--model", default="crusoe:openai/gpt-oss-120b")
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--seed-base", type=int, default=None, help="holdout: reuse a seed base (default: from clock)")
    ap.add_argument("--seeds", default=None, help="comma list: run exactly these seeds (fit personas)")
    ap.add_argument("--mock", action="store_true", help="start the app with EVOLVE_MOCK=1")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if args.seeds:
        eps, base = [(int(s), P.DOC_PERSONAS[int(s) % 4]) for s in args.seeds.split(",")], None
    else:
        eps, base = plan_episodes(args.split, args.episodes, args.seed_base, args.model)
    if base is not None:
        print(f"holdout seed base {base}: seeds {[s for s, _ in eps]}, personas "
              f"{sorted({p['name'] for _, p in eps})} (cache/personas_{base}_4.json)", flush=True)
    t0 = time.time()
    q: queue.Queue = queue.Queue()
    for i, e in enumerate(eps):
        q.put((i, e))
    results: list = [None] * len(eps)
    apps: list = []
    lock = threading.Lock()

    def worker():
        app = App(args.app_dir, mock=args.mock)
        with lock:
            apps.append(app)
        while True:
            try:
                i, (seed, persona) = q.get_nowait()
            except queue.Empty:
                return
            try:
                r = sim.run_episode(app.base, persona, seed, args.model, max_steps=args.max_steps)
            except Exception as e:
                r = {"seed": seed, "persona": persona["name"], "error": repr(e), "pass_rate": None,
                     "violation_list": [], "coverage": {}, "usage": {}, "secs": 0}
            if app.proc.poll() is not None:
                r["app_crashed"] = True
            r["cost_usd"] = round(cost(r.get("usage") or {}, args.model), 4)
            results[i] = r
            print(f"  seed {seed:>7} {persona['name']:<10} pass {r.get('pass_rate')}  viol {r.get('violations')}"
                  f"/{r.get('checks')}  {r.get('secs')}s  ${r['cost_usd']}", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(max(1, min(args.parallel, len(eps))))]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for a in apps:
            a.stop()

    report(results, args, time.time() - t0, base)


def report(results, args, wall, base):
    ok = [r for r in results if r and r.get("pass_rate") is not None]
    print(f"\n== {args.app_dir}  split={args.split}  episodes={len(results)}  model={args.model}")
    print(f"{'seed':>8} {'persona':<10} {'pass':>6} {'viol':>5} {'chk':>4} {'clean':>6} {'secs':>6}")
    for r in results:
        print(f"{r['seed']:>8} {r['persona']:<10} {str(r.get('pass_rate')):>6} {str(r.get('violations')):>5} "
              f"{str(r.get('checks')):>4} {str(r.get('steps_clean')):>6} {r.get('secs'):>6}"
              + (f"  ERROR {r['error']}" if r.get("error") else ""))
    agg: dict = {}
    for r in ok:
        for k, v in (r.get("per_invariant") or {}).items():
            a = agg.setdefault(k, {"checks": 0, "violations": 0})
            a["checks"] += v["checks"]
            a["violations"] += v["violations"]
    print("\nper invariant (violations/checks), one example each:")
    for k in sim.INVARIANTS:
        a = agg.get(k, {"checks": 0, "violations": 0})
        ex = next((v for r in ok for v in r["violation_list"] if v["invariant"] == k), None)
        print(f"  {k:<16} {a['violations']:>3}/{a['checks']:<4}" +
              (f"  e.g. seed {next(r['seed'] for r in ok if ex in r['violation_list'])} step {ex['step']}: "
               f"{ex['detail'][:160]}" if ex else ""))
    cov = {k: [0, 0] for k in sim.ACTIONS}
    feats: dict = {}
    for r in ok:
        for k, v in (r.get("coverage") or {}).items():
            cov[k][0] += v["tried"]
            cov[k][1] += v["accepted"]
        for k, v in (r.get("features") or {}).items():
            feats[k] = feats.get(k, 0) + v
    print("coverage tried/accepted: " + "  ".join(f"{k} {t}/{a}" for k, (t, a) in cov.items()))
    print("app behaviours seen: " + "  ".join(f"{k} {v}" for k, v in sorted(feats.items())))
    mean = round(sum(r["pass_rate"] for r in ok) / len(ok), 4) if ok else None
    pooled = round(1 - sum(a["violations"] for a in agg.values()) / max(1, sum(a["checks"] for a in agg.values())),
                   4)
    usd = round(sum(r.get("cost_usd", 0) for r in results if r), 4)
    print(f"MEAN pass rate {mean}  (pooled {pooled})  wall {wall:.0f}s  "
          f"mean episode {sum(r.get('secs', 0) for r in ok) / max(1, len(ok)):.0f}s  sim cost ${usd}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"app_dir": args.app_dir, "split": args.split, "seed_base": base,
                                              "model": args.model, "mean_pass_rate": mean, "pooled_pass_rate": pooled,
                                              "per_invariant": agg, "coverage": cov, "features": feats,
                                              "sim_cost_usd": usd, "wall_secs": round(wall, 1),
                                              "episodes": results}, indent=1, default=str))
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
