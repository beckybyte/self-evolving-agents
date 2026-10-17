"""FitnessSpec: fitness = sum of weighted metric terms. Tasks report flat metrics, e.g.
{"score:dev_in": 0.9, "score:ood_dev": 0.6, "tokens": 900, "complexity": 34}; the spec decides how they combine.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Term:
    metric: str
    weight: float
    scale: float = 1.0      # value used = metric / scale
    label: str = ""


@dataclass
class FitnessSpec:
    terms: list[Term] = field(default_factory=list)

    def __call__(self, metrics: dict) -> float:
        return round(sum(t.weight * metrics.get(t.metric, 0.0) / t.scale for t in self.terms), 4)

    def formula(self) -> str:
        parts = []
        for t in self.terms:
            m = t.label or (t.metric if t.scale == 1 else f"{t.metric}/{t.scale:g}")
            parts.append(f"{'-' if t.weight < 0 else '+'} {abs(t.weight):g}*{m}")
        return " ".join(parts).lstrip("+ ")

    @classmethod
    def weighted_scores(cls, split_w: dict[str, float], lam_tokens: float, **extra: tuple[float, float]):
        """split_w: {split: w} -> w*score(split); lam_tokens -> -lam*tokens/1000; extra: name=(weight, scale)."""
        terms = [Term(f"score:{s}", w, label=f"score({s})") for s, w in split_w.items()]
        if lam_tokens:
            terms.append(Term("tokens", -lam_tokens, 1000, "tokens_per_run/1000"))
        for name, (w, sc) in extra.items():
            terms.append(Term(name, w, sc))
        return cls(terms)
