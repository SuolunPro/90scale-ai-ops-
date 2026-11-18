"""Monthly DeepSeek budget ledger. Standard library only.

The ledger is a small JSON file committed to this repository:
  state/budget/YYYY-MM.json
so it survives across ephemeral GitHub Actions runners.

Hard rule: once spent >= cap_cny, no further AI call is allowed that month.
"""
from __future__ import annotations

import datetime as dt
import os

from ops.common import BJ, dump_json, load_json, now_utc

# CNY per 1,000,000 tokens. deepseek-chat list price (cache miss / output).
# Configurable through config/ops.json -> budget.prices.
DEFAULT_PRICES = {
    "deepseek-chat": {"input": 2.0, "output": 8.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
}


def month_key(now: dt.datetime | None = None) -> str:
    return (now or now_utc()).astimezone(BJ).strftime("%Y-%m")


def ledger_path(root: str, now: dt.datetime | None = None) -> str:
    return os.path.join(root, "state", "budget", f"{month_key(now)}.json")


def load_ledger(root: str, now: dt.datetime | None = None) -> dict:
    path = ledger_path(root, now)
    doc = load_json(path)
    if not isinstance(doc, dict):
        doc = {"month": month_key(now), "cap_cny": None, "entries": []}
    doc.setdefault("entries", [])
    return doc


def cost_of(model: str, usage: dict, prices: dict | None = None) -> float:
    prices = prices or DEFAULT_PRICES
    price = prices.get(model) or prices.get("deepseek-chat") or {"input": 2.0, "output": 8.0}
    pin = float(usage.get("prompt_tokens") or 0)
    pout = float(usage.get("completion_tokens") or 0)
    return round(pin / 1e6 * float(price["input"]) + pout / 1e6 * float(price["output"]), 6)


def spent(ledger: dict) -> float:
    return round(sum(float(e.get("cost_cny") or 0) for e in ledger.get("entries", [])), 6)


def summary(ledger: dict, cap_cny: float) -> dict:
    used = spent(ledger)
    calls = len(ledger.get("entries", []))
    remaining = round(max(0.0, float(cap_cny) - used), 6)
    return {
        "month": ledger.get("month"),
        "cap_cny": round(float(cap_cny), 4),
        "spent_cny": used,
        "remaining_cny": remaining,
        "calls": calls,
        "exhausted": used >= float(cap_cny),
        "used_pct": round(100.0 * used / float(cap_cny), 2) if cap_cny else 100.0,
    }


def can_call(ledger: dict, cap_cny: float, reserve_cny: float = 0.0) -> tuple[bool, str]:
    st = summary(ledger, cap_cny)
    if st["spent_cny"] >= float(cap_cny):
        return False, f"月度预算已用完（{st['spent_cny']:.4f}/{st['cap_cny']:.2f} 元），本次不调用 DeepSeek"
    if st["remaining_cny"] < float(reserve_cny):
        return False, f"剩余预算 {st['remaining_cny']:.4f} 元低于保留额 {reserve_cny:.2f} 元，本次不调用"
    return True, f"预算可用：已用 {st['spent_cny']:.4f} / {st['cap_cny']:.2f} 元"


def merge_entries(local: dict, remote: dict) -> dict:
    """并集合并：同一 (at, note) 只保留一条，避免并发写互相覆盖导致漏记费用。"""
    seen = set()
    merged = []
    for e in (remote.get("entries") or []) + (local.get("entries") or []):
        key = (str(e.get("at")), str(e.get("note")), str(e.get("cost_cny")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(e)
    merged.sort(key=lambda e: str(e.get("at")))
    out = dict(local)
    out["entries"] = merged
    return out


def commit_with_merge(root: str, cfg: dict, now: dt.datetime | None = None, attempts: int = 4) -> list[str]:
    """乐观并发提交账本：每次冲突都重新拉远端、合并、再提交。"""
    import base64
    import json as _json
    import os
    import time
    import urllib.error
    import urllib.request

    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "") or cfg.get("ops_repo", "")
    rel = f"state/budget/{month_key(now)}.json"
    path = os.path.join(root, rel)
    if not token:
        return ["本地模式：未设置 GITHUB_TOKEN，账本未提交"]
    notes = []
    for attempt in range(attempts):
        local = load_json(path) or {}
        api = f"https://api.github.com/repos/{repo}/contents/{rel}"
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "90scale-ai-ops-budget"}
        sha, remote = None, {}
        try:
            with urllib.request.urlopen(urllib.request.Request(api, headers=headers), timeout=25) as r:
                doc = _json.load(r)
            sha = doc.get("sha")
            remote = _json.loads(base64.b64decode(doc["content"]).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                notes.append(f"读取远端账本失败 HTTP {e.code}")
                return notes

        merged = merge_entries(local, remote)
        dump_json(path, merged)
        body = {"message": f"ops: budget {merged.get('month')} ({len(merged.get('entries', []))} entries)",
                "branch": "main",
                "content": base64.b64encode(open(path, "rb").read()).decode()}
        if sha:
            body["sha"] = sha
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    api, data=_json.dumps(body).encode(), headers=headers, method="PUT"), timeout=30) as r:
                notes.append(f"{rel}: committed ({r.status}) 第 {attempt + 1} 次尝试")
                return notes
        except urllib.error.HTTPError as e:
            if e.code in (409, 422) and attempt < attempts - 1:
                time.sleep(1.0 + attempt)
                continue
            notes.append(f"{rel}: 提交失败 HTTP {e.code}")
            return notes
    return notes


def record(root: str, model: str, usage: dict, note: str, prices: dict | None = None,
           cap_cny: float | None = None, now: dt.datetime | None = None) -> dict:
    ledger = load_ledger(root, now)
    if cap_cny is not None:
        ledger["cap_cny"] = float(cap_cny)
    entry = {
        "at": now_utc().isoformat(timespec="seconds"),
        "model": model,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cost_cny": cost_of(model, usage, prices),
        "note": note,
    }
    ledger["entries"].append(entry)
    dump_json(ledger_path(root, now), ledger)
    return entry
