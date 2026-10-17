"""Selectors. GreedySelector = v1 behaviour (one champion). MapElitesArchive keeps the best genome per behaviour
cell (n_llm_roles, token band, mechanism) so structurally different teams survive; sea.evolve records every child
in it, and the radical child of each generation survives there if it opens a new cell. Parent selection is still
greedy; choosing parents from the archive is the next step (interface: elites())."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .genome import structure_signature


def descriptor(g: dict, metrics: dict) -> str:
    n_llm = sum(r["kind"] == "llm" for r in g["roles"])
    tok = metrics.get("tokens", 0)
    band = 0 if tok <= 0 else int(math.log2(max(tok, 1) / 500) + 1) if tok > 500 else 0
    mech = g.get("mechanism") or structure_signature(g)
    return f"llm{n_llm}|tok{band}|{mech}"


class MapElitesArchive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.cells: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    def add(self, gid: str, gdir: str, g: dict, metrics: dict, fitness: float, radical: bool = False) -> bool:
        cell = descriptor(g, metrics)
        cur = self.cells.get(cell)
        new = cur is None or fitness > cur["fitness"]
        if new:
            self.cells[cell] = {"id": gid, "dir": str(gdir), "fitness": fitness, "radical": radical}
            self.path.write_text(json.dumps(self.cells, indent=1))
        return new

    def is_new_cell(self, g: dict, metrics: dict) -> bool:
        return descriptor(g, metrics) not in self.cells

    def elites(self) -> list[dict]:
        return sorted(({"cell": c, **v} for c, v in self.cells.items()), key=lambda x: -x["fitness"])
