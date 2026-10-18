"""Minimal Crusoe (OpenAI-compatible) client with hard timeouts and a per-request time budget.

Every call returns None on any problem (mock mode, no key, timeout, HTTP error, unparsable output) so the
caller always falls back to rules.
"""
import json
import os
import re
import threading
import time
import urllib.request

_local = threading.local()
STATS = {"calls": 0, "ok": 0, "fail": 0, "seconds": 0.0}
_stats_lock = threading.Lock()

REQUEST_BUDGET_S = 48.0   # whole HTTP request must answer within 60 s
CALL_TIMEOUT_S = 25.0


def mock():
    return os.environ.get("EVOLVE_MOCK", "") not in ("", "0", "false", "False")


def _key():
    k = os.environ.get("CRUSOE_API", "")
    if not k:
        try:
            with open(os.path.expanduser("~/.zshrc")) as f:
                for line in f:
                    m = re.match(r"\s*export\s+CRUSOE_API=[\"']?([^\"'\s]+)", line)
                    if m:
                        k = m.group(1)
        except OSError:
            pass
    return k


def start_request():
    _local.t0 = time.time()


def _remaining():
    t0 = getattr(_local, "t0", None)
    if t0 is None:
        return CALL_TIMEOUT_S
    return REQUEST_BUDGET_S - (time.time() - t0)


def strip_think(s):
    if not s:
        return ""
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    if "</think>" in s:
        s = s.split("</think>", 1)[1]
    return s.strip()


def chat(messages, smart=False, max_tokens=4000, temperature=0.3):
    """Returns the model's text (think stripped) or None."""
    if mock():
        return None
    key = _key()
    if not key:
        return None
    base = os.environ.get("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1").rstrip("/")
    fast = os.environ.get("MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
    models = [os.environ.get("MODEL_SMART", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"), fast] if smart else [fast]
    for n, model in enumerate(models):
        cap = 18.0 if (smart and n == 0) else CALL_TIMEOUT_S   # leave room for the fast-model retry
        timeout = min(cap, _remaining())
        if timeout < 3:
            return None
        body = {"model": model, "messages": messages, "max_tokens": max(4000, max_tokens),
                "temperature": temperature}
        req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            text = strip_think(data["choices"][0]["message"].get("content") or "")
            _record(True, time.time() - t)
            if text:
                return text
        except Exception:
            _record(False, time.time() - t)
    return None


def _record(ok, secs):
    with _stats_lock:
        STATS["calls"] += 1
        STATS["ok" if ok else "fail"] += 1
        STATS["seconds"] += secs


def chat_json(messages, smart=False):
    """Parse the first JSON object/array in the reply, or None."""
    text = chat(messages, smart=smart)
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    pairs = sorted((("{", "}"), ("[", "]")), key=lambda p: (text.find(p[0]) == -1, text.find(p[0])))
    for opener, closer in pairs:
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None
