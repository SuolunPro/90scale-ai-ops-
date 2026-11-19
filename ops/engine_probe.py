"""只读探测私人引擎的「逐场停售 / 冻结」实现痕迹。

安全约束：**绝不输出源码内容**，只输出布尔值与字段名，用于确认能力存在与否。
"""
from __future__ import annotations

import base64
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C  # noqa: E402

REPO = "SuolunPro/90scale-engine"
MARKERS = {
    "sale_end": ["sale_end", "stop_sale_at", "sale_end_at"],
    "freeze": ["frozen", "freeze", "冻结"],
    "history_guard": ["history", "snapshot", "immutable", "不可变"],
    "predicted_at": ["predicted_at"],
}
FILES = [".github/workflows/engine4.yml", "engine4/engine.py", "engine4/settle.py", "engine4/aggregate.py"]


def _get(path: str, token: str, timeout: int = 20):
    import urllib.error
    import urllib.request
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.load(r)
        return base64.b64decode(doc["content"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    token = os.getenv("ENGINE4_REPAIR_TOKEN", "")
    if not token:
        print("FATAL: 缺少 ENGINE4_REPAIR_TOKEN")
        return 2

    out = {"files_present": {}, "markers": {}, "snapshot_fields": {}}
    for path in FILES:
        text = _get(path, token)
        out["files_present"][path] = bool(text)
        if not text:
            continue
        low = text.lower()
        hits = {}
        for group, words in MARKERS.items():
            hits[group] = sum(1 for w in words if w.lower() in low)
        out["markers"][path] = hits

    snap = _get("data/engine4/latest.json", token)
    if snap:
        try:
            doc = json.loads(snap)
            keys = set()
            for m in (doc.get("matches") or [])[:10]:
                keys |= set(m.keys())
            out["snapshot_fields"] = {
                "matches": len(doc.get("matches") or []),
                "has_sale_end": any(k in keys for k in ("sale_end", "stop_sale_at", "sale_end_at")),
                "has_frozen": "frozen" in keys or "freeze" in keys,
                "has_predicted_at": "predicted_at" in keys,
                "sale_day": doc.get("sale_day"),
            }
        except ValueError:
            out["snapshot_fields"] = {"error": "parse"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
