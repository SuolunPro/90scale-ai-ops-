"""受控故障恢复：固定的重试/重跑动作必须显式授权，且受停售窗口与频率限制。

只能在 workflow_dispatch 里由人触发（不在 cron 里自动跑）。
每次执行都会写 state/recovery/history.jsonl，包含操作者、授权串、参数、结果。

允许的动作（白名单）：
  dispatch_engine4    重新触发私人引擎的预测工作流（写生产 → 必须授权 + 避开销售窗口）
  dispatch_site_sync  重新触发网站数据同步工作流
  rerun_failed_run    重跑指定的一次失败运行（仅限 workflow 重新运行，不改代码）

代码修复不走这里，必须走 ops/pr.py 生成独立 PR 供审核。
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

from ops import ghcommit  # noqa: E402
from ops import common as C  # noqa: E402

CONFIRM_PHRASE = "YES-I-APPROVE"
ALLOWED = {"dispatch_engine4", "dispatch_site_sync", "rerun_failed_run"}


def _api(method: str, path: str, token: str, body=None):
    url = "https://api.github.com" + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, (r.read()[:200] if r.status != 204 else b"")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]


def stop_sale_bounds(cfg: dict | None) -> tuple[tuple[int, int], tuple[int, int]]:
    rec = (cfg or {}).get("recovery") or {}
    window = rec.get("stop_sale_window_bj") or ["08:45", "22:45"]

    def parse(x: str) -> tuple[int, int]:
        h, m = x.split(":")
        return int(h), int(m)

    return parse(window[0]), parse(window[1])


def in_stop_sale_window(now, cfg: dict | None = None) -> bool:
    bj = now.astimezone(C.BJ)
    start, end = stop_sale_bounds(cfg)
    return start <= (bj.hour, bj.minute) <= end


def check_rate(history: list, now, limit: int) -> str:
    recent = [h for h in history
              if C.age_min(C.parse_ts(h.get("at")), now) is not None
              and C.age_min(C.parse_ts(h.get("at")), now) < 60]
    if len(recent) >= limit:
        return f"最近 60 分钟已执行 {len(recent)} 次恢复，达到上限 {limit}"
    return ""


def preflight(cfg: dict, action: str, confirm: str, now, history: list) -> list[str]:
    """返回拒绝原因列表；空列表表示允许执行。"""
    problems = []
    rec = cfg.get("recovery") or {}
    if not rec.get("enabled", False):
        problems.append("recovery 未在 config 中启用")
    if confirm != CONFIRM_PHRASE:
        problems.append(f"缺少授权串（需要在输入框填写 {CONFIRM_PHRASE}）")
    if action not in ALLOWED:
        problems.append(f"动作不在白名单：{action}")
    if in_stop_sale_window(now, cfg):
        problems.append("当前处于停售保护窗口（北京时间 08:45-22:45），拒绝任何会写生产的恢复动作；"
                        "如确属紧急，请等窗口结束或由人工在 GitHub 上直接操作并留痕")
    rate = check_rate(history, now, int(rec.get("max_per_hour", 2)))
    if rate:
        problems.append(rate)
    return problems


def execute(cfg: dict, action: str, params: dict, token: str) -> dict:
    engine = cfg["engine_repo"]
    site = cfg["site_repo"]
    if action == "dispatch_engine4":
        wf = (cfg.get("engine") or {}).get("workflow_file", "engine4.yml")
        code, body = _api("POST", f"/repos/{engine}/actions/workflows/{wf}/dispatches",
                          token, {"ref": "main"})
        return {"action": action, "repo": engine, "http": code, "ok": code in (201, 204)}
    if action == "dispatch_site_sync":
        code, body = _api("POST", f"/repos/{site}/actions/workflows/sync-engine4.yml/dispatches",
                          token, {"ref": "main"})
        return {"action": action, "repo": site, "http": code, "ok": code in (201, 204)}
    if action == "rerun_failed_run":
        run_id = int(params.get("run_id", 0))
        if not run_id:
            return {"action": action, "ok": False, "error": "缺少 run_id"}
        code, body = _api("POST", f"/repos/{site}/actions/runs/{run_id}/rerun", token, {})
        return {"action": action, "run_id": run_id, "http": code, "ok": code in (201, 204)}
    return {"action": action, "ok": False, "error": "unsupported"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--action", required=True, choices=sorted(ALLOWED))
    ap.add_argument("--confirm", default="")
    ap.add_argument("--ticket", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--run-id", type=int, default=0)
    ap.add_argument("--actor", default=os.getenv("GITHUB_ACTOR", "unknown"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    now = C.now_utc()
    hist_path = os.path.join(args.root, "state", "recovery", "history.jsonl")
    history = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as f:
            history = [json.loads(x) for x in f if x.strip()]

    problems = preflight(cfg, args.action, args.confirm, now, history)
    record = {"at": C.iso(now), "action": args.action, "actor": args.actor,
              "ticket": args.ticket, "reason": args.reason, "run_id": args.run_id,
              "dry_run": args.dry_run, "allowed": not problems, "problems": problems}
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if problems:
        print("REFUSED: " + "；".join(problems))
        if not args.dry_run:  # 拒绝也要留痕
            os.makedirs(os.path.dirname(hist_path), exist_ok=True)
            with open(hist_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            ghcommit.commit_files(args.root, cfg, ["state/recovery/history.jsonl"],
                                  f"ops: recovery refused {args.action} by {args.actor}")
        return 0 if args.dry_run else 1
    if args.dry_run:
        print("DRY-RUN: 通过预检，未实际执行")
        return 0

    token = os.getenv("OPS_TOKEN", "")
    if not token:
        print("REFUSED: 缺少 OPS_TOKEN（需要写权限的运维 token）")
        return 1
    result = execute(cfg, args.action, {"run_id": args.run_id}, token)
    record["result"] = result
    os.makedirs(os.path.dirname(hist_path), exist_ok=True)
    with open(hist_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    ghcommit.commit_files(args.root, cfg, ["state/recovery/history.jsonl"],
                          f"ops: recovery {args.action} by {args.actor}")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
