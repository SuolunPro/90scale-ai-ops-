"""跨触发来源去重：同一工作流在短时间内只允许跑一次完整巡检。

场景：GitHub schedule 与 Cloudflare external_tick 可能在同一分钟内同时触发，
导致重复巡检、重复消耗（虽然只读，但会重复调 GitHub API 与 DeepSeek）。

做法：查本工作流最近运行；若在窗口内有别的运行（排除自己）→ skip=true。
不依赖任何本地状态文件，天然跨触发来源生效。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C  # noqa: E402


def recent_runs(repo: str, token: str, workflow: str, limit: int = 10) -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?per_page={limit}"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("workflow_runs", [])
    except Exception:  # noqa: BLE001
        return []


def decide(runs: list[dict], self_id: str, now, window_min: float) -> tuple[bool, str]:
    for r in runs:
        if str(r.get("id")) == str(self_id):
            continue
        created = C.parse_ts(r.get("created_at"))
        age = C.age_min(created, now)
        if age is None or age > window_min:
            continue
        return False, f"最近 {age:.1f} 分钟前已有运行 {r.get('id')}（{r.get('event')}）"
    return True, "窗口内没有其它运行"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", default="ai-ops-inspect.yml")
    ap.add_argument("--window-min", type=float, default=5.0)
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "SuolunPro/90scale-ai-ops-"))
    ap.add_argument("--self-id", default=os.getenv("GITHUB_RUN_ID", ""))
    args = ap.parse_args(argv)

    token = os.getenv("GITHUB_TOKEN", "")
    now = C.now_utc()
    runs = recent_runs(args.repo, token, args.workflow) if token else []
    proceed, why = decide(runs, args.self_id, now, args.window_min)
    skip = not proceed
    print(json.dumps({"skip": skip, "why": why, "self": args.self_id,
                      "window_min": args.window_min, "at": C.iso(now)}, ensure_ascii=False))
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"skip={'true' if skip else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
