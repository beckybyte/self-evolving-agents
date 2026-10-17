"""ask_human, non-blocking. Questions go to <inbox>/questions.jsonl with the asker's default answer; the caller
continues on that default immediately (and records it as an assumption). A human answers whenever they like by
appending to <inbox>/answers.jsonl, or with the CLI:

  python -m sea.human --inbox runs/x/inbox list
  python -m sea.human --inbox runs/x/inbox answer Q3 "only for students, mobile first"
  python -m sea.human --inbox runs/x/inbox rate gen01a1_c2 4 "too many features"

Answers are folded back in at generation boundaries (sea.evolve -> Task.refresh), never mid-run.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", " ", q.lower()).strip()


class HumanChannel:
    def __init__(self, inbox: str | Path):
        self.dir = Path(inbox)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.qf, self.af, self.rf = self.dir / "questions.jsonl", self.dir / "answers.jsonl", self.dir / "ratings.jsonl"
        self._lock = threading.Lock()

    @staticmethod
    def _read(f: Path) -> list[dict]:
        return [json.loads(l) for l in open(f) if l.strip()] if f.exists() else []

    def questions(self) -> list[dict]:
        return self._read(self.qf)

    def answers(self) -> dict[str, str]:
        """qid -> latest answer."""
        return {a["qid"]: a["answer"] for a in self._read(self.af)}

    def ratings(self) -> list[dict]:
        return self._read(self.rf)

    def ask(self, question: str, default: str, asker: str, context: str = "", tag: str = "") -> dict:
        """Post (or find) a question. Returns {"qid", "answer", "answered": bool}; answer = human's or the default."""
        with self._lock:
            qs = self.questions()
            for q in qs:
                if _norm(q["question"]) == _norm(question):
                    ans = self.answers().get(q["qid"])
                    return {"qid": q["qid"], "answer": ans if ans is not None else q["default"], "answered": ans is not None}
            qid = f"Q{len(qs) + 1}"
            with open(self.qf, "a") as f:
                f.write(json.dumps({"qid": qid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "asker": asker, "tag": tag,
                                    "question": question, "default": default, "context": context[:600]},
                                   ensure_ascii=False) + "\n")
        return {"qid": qid, "answer": default, "answered": False}

    def answer(self, qid: str, text: str):
        with self._lock, open(self.af, "a") as f:
            f.write(json.dumps({"qid": qid, "answer": text, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False) + "\n")

    def rate(self, genome_id: str, score: float, comment: str = ""):
        with self._lock, open(self.rf, "a") as f:
            f.write(json.dumps({"genome_id": genome_id, "score": score, "comment": comment,
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False) + "\n")

    def answered_pairs(self) -> list[dict]:
        ans = self.answers()
        return [{**q, "answer": ans[q["qid"]]} for q in self.questions() if q["qid"] in ans]

    def open_questions(self) -> list[dict]:
        ans = self.answers()
        return [q for q in self.questions() if q["qid"] not in ans]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True)
    ap.add_argument("cmd", choices=["list", "answer", "rate"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    h = HumanChannel(a.inbox)
    if a.cmd == "list":
        ans = h.answers()
        for q in h.questions():
            print(f"{q['qid']} [{q['asker']}] {q['question']}\n    default: {q['default']}\n    answer : {ans.get(q['qid'], '(open)')}")
    elif a.cmd == "answer":
        h.answer(a.args[0], " ".join(a.args[1:]))
    else:
        h.rate(a.args[0], float(a.args[1]), " ".join(a.args[2:]))
