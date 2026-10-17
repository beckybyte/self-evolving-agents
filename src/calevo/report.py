"""W6 report: final test-set table + evolution curves -> REPORT.md, report_curves.png.

Usage: PYTHONPATH=src python3 -m calevo.report [--run-missing]
Test splits are only ever evaluated here (never inside evolution).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .runner import ROOT, evaluate

TEST_SPLITS = ("test_in", "ood_test", "test_v2")
WORKERS = ("qwen3-30b-a3b", "qwen3-8b")
RUNS = {"evo_full": "Evolved, tools", "evo_prompt": "Evolved, prompt-only",
        "evo_full_leaky": "Evolved, tools, LEAKY doc (reference only)",
        "evo_full_8b": "Evolved, tools", "evo_full_8b_v2guard": "Evolved, tools, +dev_v2 in fitness",
        "evo_full_8b_v2guard_lam005": "Evolved, tools, +dev_v2, lambda=0.005",
        "evo_full_diverse": "Evolved, tools, +C-type prefs in fitness"}


def run_state(run: str) -> dict:
    st = ROOT / "runs" / run / "state.json"
    return json.loads(st.read_text()) if st.exists() else {}


def best_genome(run: str):
    st = ROOT / "runs" / run / "state.json"
    if not st.exists():
        return None
    p = Path(json.loads(st.read_text())["parent"])
    return p if p.is_absolute() else ROOT / p


def history(run: str) -> list[dict]:
    f = ROOT / "runs" / run / "history.jsonl"
    return [json.loads(l) for l in open(f)] if f.exists() else []


def entries():
    """(label, genome dir, output key, worker, finished?)"""
    out = []
    for w in WORKERS:
        out += [("P0 single prompt", ROOT / "genomes" / "P0", f"P0_{w}", w, True),
                ("S0 fixed-DSL solver", ROOT / "genomes" / "S0", f"S0_{w}", w, True)]
        for run, label in RUNS.items():
            st = run_state(run)
            if st.get("worker") == w and (g := best_genome(run)):
                out.append((f"{label} [{run}]", g, f"{run}__{g.name}", w, bool(st.get("done")) or "leaky" in run))
    return out


def main(run_missing: bool):
    rows = []
    for label, gdir, key, worker, finished in entries():
        rec = {"label": label, "genome": str(gdir.relative_to(ROOT)), "worker": worker,
               "status": "" if finished else " (still evolving - not evaluated)"}
        for s in TEST_SPLITS:
            out = ROOT / "runs" / "final" / f"{key}_{s}"
            rp = out / "report.json"
            if not rp.exists() and run_missing and finished:
                evaluate(gdir, s, worker, out, workers=16)
            if rp.exists():
                r = json.loads(rp.read_text())
                rec[s] = (r["score"], r["tokens_per_sample"])
        rows.append(rec)

    lines = ["# Calendar-Evo report", "", "Score = official hard pass (0/1); OOD rows add soft-pref credit "
             "(0.5 + 0.5 x prefs met). Tokens = all LLM calls per sample.", "",
             "| worker | team | test_in | ood_test (unseen pref types) | test_v2 (long convos) |", "|---|---|---|---|---|"]
    for r in rows:
        cells = [f"{r[s][0]:.3f} ({r[s][1]:.0f} tok)" if s in r else "-" for s in TEST_SPLITS]
        lines.append(f"| {r['worker']} | {r['label']}{r['status']} | " + " | ".join(cells) + " |")
    for run, label in RUNS.items():
        h = history(run)
        if not h:
            continue
        lines += ["", f"## {label} - evolution history ({run})", "",
                  "| gen | attempt | genome | fitness | dev_in | ood_dev | tok | roles | accepted | notes |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for x in h:
            lines.append(f"| {x['gen']} | {x.get('attempt', '')} | {x['id']} | {x['fitness']} | {x['dev_in']} | {x['ood_dev']} | "
                         f"{x['tokens']:.0f} | {'+'.join(x['roles'])} | {'yes' if x['accepted'] else ''} | "
                         f"{x['notes'][:160].replace('|', '/')} |")
    (ROOT / "REPORT.md").write_text("\n".join(lines) + "\n")
    plot()
    print("\n".join(lines[:12]))


def plot():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    for run, label in RUNS.items():
        h = history(run)
        if not h:
            continue
        acc = [x for x in h if x["accepted"]]
        gens = [x["gen"] for x in acc]
        style = "--" if "leaky" in run else "-"
        axes[0].plot(gens, [x["fitness"] for x in acc], style, marker="o", label=label)
        axes[1].plot(gens, [x["dev_in"] for x in acc], style, marker="o", label=f"{run} dev_in")
        axes[1].plot(gens, [x["ood_dev"] for x in acc], style, marker="x", label=f"{run} ood_dev")
        axes[2].plot(gens, [x["tokens"] for x in acc], style, marker="o", label=label)
        axes[0].scatter([x["gen"] for x in h if not x["accepted"]], [x["fitness"] for x in h if not x["accepted"]],
                        s=10, alpha=0.4)
    for ax, t in zip(axes, ["fitness of accepted parent (dots = rejected children)", "score", "tokens / sample"]):
        ax.set_title(t)
        ax.set_xlabel("generation")
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=7)
    axes[1].legend(fontsize=6)
    fig.tight_layout()
    fig.savefig(ROOT / "report_curves.png", dpi=120)


if __name__ == "__main__":
    main("--run-missing" in sys.argv)
