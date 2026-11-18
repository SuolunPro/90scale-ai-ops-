"""受限恢复 V1.3：把「运维动作」和「生产代码修改」彻底分开。

两类通道
--------
A. 运维恢复（本文件）：只触发**已存在的**工作流，不写任何数据文件，
   因此不会改写已冻结的赛事数据。按计划（plan）声明前置条件，避免盲触发。
B. 代码修复（ops/fix_pr.py）：跑测试 → 建独立分支 → 提交 → 开 PR → 等人工审核。
   本文件永远不执行代码修改，也永远不合并 PR。

停售保护重新设计
----------------
旧设计：08:45-22:45 一律禁止恢复 → 反向限制，窗口内出故障无法救。
新设计按动作类别判定：
  * operational + allow_in_sale=true  → 窗口内允许（带授权串），如重触发预测/同步
  * operational + allow_in_sale=false → 窗口内禁止（会改变对外可见数据的行为）
  * writes_frozen_data=true           → 一律禁止（配置层面直接拒绝，防止误配）
  * class=code                        → 本通道一律禁止，只能走 PR

授权与审计
----------
* 必须显式 `--confirm YES-I-APPROVE`
* 每个 plan 有独立的每小时次数上限
* 允许/拒绝都写 state/recovery/history.jsonl
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
CLASS_OPERATIONAL = "operational"
CLASS_CODE = "code"
EXECUTABLE = {"dispatch_engine4", "dispatch_site_sync", "rerun_failed_run"}


# --------------------------------------------------------------------- config
def plans(cfg: dict) -> dict:
    return (cfg.get("recovery_plans") or {})


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


# ------------------------------------------------------------------ preflight
def check_preconditions(plan: dict, status_doc: dict | None) -> list[str]:
    need = plan.get("preconditions") or {}
    if not need:
        return []
    if not isinstance(status_doc, dict):
        return ["缺少巡检状态文件，无法核对前置条件"]
    got = {c.get("id"): c.get("status") for c in status_doc.get("checks", [])}
    problems = []
    for check_id, allowed in need.items():
        actual = got.get(check_id)
        if actual not in allowed:
            problems.append(f"前置条件不满足：{check_id}={actual or '缺失'}，需为 {'/'.join(allowed)}")
    return problems


def recent_count(history: list, plan_name: str, now, window_min: int = 60) -> int:
    n = 0
    for h in history:
        if h.get("plan") != plan_name or not h.get("allowed"):
            continue
        age = C.age_min(C.parse_ts(h.get("at")), now)
        if age is not None and age < window_min:
            n += 1
    return n


def preflight(cfg: dict, plan_name: str, confirm: str, now,
              history: list | None = None, status_doc: dict | None = None) -> list[str]:
    """返回拒绝原因；空列表表示允许执行。"""
    history = history or []
    problems: list[str] = []
    rec = cfg.get("recovery") or {}
    plan = plans(cfg).get(plan_name)

    if not rec.get("enabled", False):
        problems.append("recovery 未在 config 中启用")
    if not plan:
        problems.append(f"未定义的恢复计划：{plan_name}（可用：{sorted(plans(cfg))}）")
        return problems
    if confirm != CONFIRM_PHRASE:
        problems.append(f"缺少授权串（需要在输入框填写 {CONFIRM_PHRASE}）")

    klass = plan.get("class")
    action = plan.get("action")
    if klass == CLASS_CODE:
        problems.append("该计划属于生产代码修改，必须走 ops/fix_pr.py 的 PR 通道，恢复通道拒绝执行")
    if plan.get("writes_frozen_data"):
        problems.append("该计划会写已冻结赛事数据，永久禁止自动执行")
    if klass == CLASS_OPERATIONAL and action not in (EXECUTABLE | {"none"}):
        problems.append(f"动作不在白名单：{action}")

    if in_stop_sale_window(now, cfg) and not plan.get("allow_in_sale", False):
        problems.append("当前处于停售保护窗口（北京时间 08:45-22:45），该计划不允许在窗口内执行")

    problems += check_preconditions(plan, status_doc)

    limit = int(plan.get("max_per_hour", rec.get("max_per_hour", 1)))
    used = recent_count(history, plan_name, now)
    if used >= limit:
        problems.append(f"计划 {plan_name} 最近 60 分钟已执行 {used} 次，达到上限 {limit}")

    return problems


# -------------------------------------------------------------------- execute
def _api(method: str, path: str, token: str, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, (r.read()[:200] if r.status != 204 else b"")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]


def execute(cfg: dict, plan: dict, token: str, run_id: int = 0) -> dict:
    action = plan.get("action")
    engine = cfg["engine_repo"]
    site = cfg["site_repo"]
    if action == "none":
        return {"action": action, "ok": True, "skipped": True,
                "note": "该计划仅要求报告，不执行任何动作"}
    if action == "dispatch_engine4":
        wf = (cfg.get("engine") or {}).get("workflow_file", "engine4.yml")
        code, _ = _api("POST", f"/repos/{engine}/actions/workflows/{wf}/dispatches", token, {"ref": "main"})
        return {"action": action, "repo": engine, "http": code, "ok": code in (201, 204)}
    if action == "dispatch_site_sync":
        code, _ = _api("POST", f"/repos/{site}/actions/workflows/sync-engine4.yml/dispatches", token, {"ref": "main"})
        return {"action": action, "repo": site, "http": code, "ok": code in (201, 204)}
    if action == "rerun_failed_run":
        if not run_id:
            return {"action": action, "ok": False, "error": "缺少 run_id"}
        code, _ = _api("POST", f"/repos/{site}/actions/runs/{run_id}/rerun", token, {})
        return {"action": action, "run_id": run_id, "http": code, "ok": code in (201, 204)}
    return {"action": action, "ok": False, "error": "unsupported"}


def _audit(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--status", default="state/latest.json")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--confirm", default="")
    ap.add_argument("--ticket", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--run-id", type=int, default=0)
    ap.add_argument("--actor", default=os.getenv("GITHUB_ACTOR", "unknown"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-plans", action="store_true")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    if not cfg:
        print("FATAL: 配置不可读")
        return 2
    if args.list_plans:
        for name, p in plans(cfg).items():
            print(f"{name:22} class={p.get('class'):12} action={p.get('action'):20} "
                  f"allow_in_sale={p.get('allow_in_sale')} preconditions={p.get('preconditions')}")
        return 0

    now = C.now_utc()
    status_doc = C.load_json(os.path.join(args.root, args.status))
    hist_path = os.path.join(args.root, "state", "recovery", "history.jsonl")
    history = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as f:
            history = [json.loads(x) for x in f if x.strip()]

    plan = plans(cfg).get(args.plan) or {}
    problems = preflight(cfg, args.plan, args.confirm, now, history, status_doc)
    record = {"at": C.iso(now), "plan": args.plan, "action": plan.get("action"),
              "actor": args.actor, "ticket": args.ticket, "reason": args.reason,
              "run_id": args.run_id, "dry_run": args.dry_run,
              "allowed": not problems, "problems": problems}
    print(json.dumps(record, ensure_ascii=False, indent=2))

    if problems:
        print("REFUSED: " + "；".join(problems))
        if not args.dry_run:
            _audit(hist_path, record)
            ghcommit.commit_files(args.root, cfg, ["state/recovery/history.jsonl"],
                                  f"ops: recovery refused {args.plan}")
        return 0 if args.dry_run else 1
    if args.dry_run:
        print("DRY-RUN: 通过预检，未实际执行")
        return 0

    token = os.getenv("OPS_TOKEN") or os.getenv("ENGINE4_REPAIR_TOKEN", "")
    if not token:
        print("REFUSED: 缺少 OPS_TOKEN/ENGINE4_REPAIR_TOKEN")
        return 1
    result = execute(cfg, plan, token, run_id=args.run_id)
    record["result"] = result
    _audit(hist_path, record)
    ghcommit.commit_files(args.root, cfg, ["state/recovery/history.jsonl"],
                          f"ops: recovery {args.plan} by {args.actor}")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
