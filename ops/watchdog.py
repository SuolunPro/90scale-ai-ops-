"""预测引擎漏跑看门狗（V1.4）。

只做三件事：
1. 判断 engine4.yml 的计划点是否漏跑（结合最近运行与当日快照新鲜度）；
2. 在满足全部安全条件时，**最多**补触发一次（受冷却、每日上限、停售窗口、活动运行约束）；
3. 记录触发原因、时间、Run ID，并在下一轮解析运行结果与网站同步状态。

默认 dry-run；只有 workflow_dispatch 显式 apply 才可能真正触发。
绝不修改预测算法、历史冻结数据或结算链路——这里只会调用 dispatches API。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C  # noqa: E402

PENDING = "state/watchdog/pending.json"
HISTORY = "state/watchdog/history.jsonl"


# ------------------------------------------------------------------ decisions
def decide(ctx: dict) -> dict:
    """纯函数：给定上下文，返回是否允许补触发及原因。

    manual=True 表示人工显式授权（workflow_dispatch + 授权串）。
    停售窗口对自动模式一律拦截；对人工授权模式可按配置放行，并记录 override。
    """
    cfg = ctx.get("cfg") or {}
    manual = bool(ctx.get("manual"))
    blocked = []
    overrides = []

    if not cfg.get("enabled", False):
        blocked.append("watchdog 未启用")
    active = ctx.get("active_runs") or []
    if cfg.get("require_no_active_run", True) and active:
        blocked.append(f"引擎有 {len(active)} 个运行中的任务：{[a['id'] for a in active][:3]}")
    missed = ctx.get("missed") or []
    if not missed:
        blocked.append("窗口内没有漏跑计划点")
    if ctx.get("in_sale_window") and cfg.get("block_in_sale_window", True):
        stale = ctx.get("fresh_status") == "fail"
        if manual and cfg.get("manual_override_in_sale", True):
            overrides.append("sale_window: 人工授权放行")
        elif stale and cfg.get("allow_in_sale_when_stale", False):
            overrides.append("sale_window: 数据已过期(fail)，按方案 B 放行自动补跑")
        else:
            blocked.append("当前处于停售窗口（08:45-22:45），不自动补跑")
    if ctx.get("engine_status") not in ("warn", "fail") and ctx.get("fresh_status") not in ("warn", "fail"):
        blocked.append("引擎数据仍然新鲜，无需补跑")
    cooldown = float(cfg.get("cooldown_min", 90))
    last = ctx.get("last_auto_age_min")
    if last is not None and last < cooldown:
        blocked.append(f"冷却未结束：上次自动补跑在 {last:.0f} 分钟前（需 {cooldown:.0f} 分钟）")
    cap = int(cfg.get("max_per_day", 3))
    if int(ctx.get("today_auto_count", 0)) >= cap:
        blocked.append(f"今日自动补跑已达上限 {cap} 次")

    return {"should_trigger": not blocked, "blocked_by": blocked,
            "overrides": overrides, "manual": manual,
            "missed_points": missed, "at": C.iso(ctx["now"])}


# ------------------------------------------------------------------- fetching
def _api(url: str, token: str, method: str = "GET", body=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": C.UA}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def missed_points(cfg: dict, runs: list[dict], now) -> list[str]:
    eng = cfg.get("engine") or {}
    grace = dt.timedelta(minutes=float(eng.get("grace_min", 20)))
    lookback = dt.timedelta(minutes=float((cfg.get("watchdog") or {}).get("lookback_min", 90)))
    bj_now = now.astimezone(C.BJ)
    stamps = [C.parse_ts(r.get("created_at")) for r in runs]
    stamps = [s for s in stamps if s]
    missed = []
    for hhmm in eng.get("schedule_bj", []):
        hh, mm = (int(x) for x in hhmm.split(":"))
        point = bj_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if point > bj_now or (bj_now - point) > lookback:
            continue
        if not any(point - dt.timedelta(minutes=2) <= s <= point + grace for s in stamps):
            missed.append(hhmm)
    return missed


def load_history(root: str) -> list[dict]:
    path = os.path.join(root, HISTORY)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def append_history(root: str, record: dict) -> None:
    path = os.path.join(root, HISTORY)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def resolve_pending(root: str, cfg: dict, token: str, now) -> dict | None:
    """上一轮触发的运行若已结束，补齐结论与网站同步状态。"""
    path = os.path.join(root, PENDING)
    pending = C.load_json(path)
    if not isinstance(pending, dict) or not pending.get("run_id"):
        return None
    repo = cfg["engine_repo"]
    wf = (cfg.get("engine") or {}).get("workflow_file", "engine4.yml")
    code, doc = _api(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs/{pending['run_id']}", token)
    if code != 200:
        # 某些情况下 workflow 维度的 run 端点会 404，退回仓库维度
        code2, doc2 = _api(f"https://api.github.com/repos/{repo}/actions/runs/{pending['run_id']}", token)
        if code2 == 200:
            code, doc = code2, doc2
    if code != 200:
        return {"pending": True, "run_id": pending["run_id"], "http": code,
                "error": str(doc.get("error"))[:200]}
    if doc.get("status") != "completed":
        return {"pending": True, "run_id": pending["run_id"], "status": doc.get("status")}
    site = C.http_get(cfg["site"] + "/data/engine4/latest.json", timeout=20, retries=1)
    site_doc = C.as_json(site) or {}
    record = {
        "at": C.iso(now), "phase": "resolved", "run_id": pending["run_id"],
        "trigger_reason": pending.get("reason"), "missed_points": pending.get("missed_points"),
        "conclusion": doc.get("conclusion"),
        "started_at": doc.get("run_started_at"), "updated_at": doc.get("updated_at"),
        "site_sale_day": site_doc.get("sale_day"), "site_generated_at": site_doc.get("generated_at"),
        "site_http": site.get("status"),
    }
    already = any(h.get("phase") == "resolved" and str(h.get("run_id")) == str(pending["run_id"])
                  for h in load_history(root))
    if not already:
        append_history(root, record)
    try:
        os.remove(path)
    except OSError:
        pass
    return {"resolved": record}


def gather(cfg: dict, token: str, root: str, now) -> dict:
    repo = cfg["engine_repo"]
    wf = (cfg.get("engine") or {}).get("workflow_file", "engine4.yml")
    code, doc = _api(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?per_page=30", token)
    runs = doc.get("workflow_runs", []) if code == 200 else []
    state = C.load_json(os.path.join(root, "state", "latest.json")) or {}
    status = {c.get("id"): c.get("status") for c in state.get("checks", [])}
    history = load_history(root)
    today = now.astimezone(C.BJ).date()
    auto_today = [h for h in history if h.get("phase") == "dispatched"
                  and (C.parse_ts(h.get("at")) or now).astimezone(C.BJ).date() == today]
    last_dispatch = max((C.parse_ts(h["at"]) for h in auto_today if h.get("at")), default=None)
    bj_now = now.astimezone(C.BJ)
    return {
        "runs_error": doc.get("error") if code != 200 else None,
        "runs": runs,
        "active_runs": [r for r in runs if r.get("status") in ("queued", "in_progress", "waiting")],
        "missed": missed_points(cfg, runs, now),
        "engine_status": status.get("engine_runs"),
        "fresh_status": status.get("engine_fresh"),
        "site_sync_status": status.get("site_data_sync"),
        "in_sale_window": (8, 45) <= (bj_now.hour, bj_now.minute) <= (22, 45),
        "last_auto_age_min": C.age_min(last_dispatch, now) if last_dispatch else None,
        "today_auto_count": len(auto_today),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--apply", action="store_true", help="允许真正 dispatch（需 workflow 显式授权）")
    ap.add_argument("--auto", action="store_true", help="自动模式：按 config.watchdog.auto_apply 决定是否 dispatch")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--resolve-only", action="store_true",
                    help="只补齐上一轮 Run 的结论与网站同步状态，绝不触发引擎")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    if not cfg:
        print("FATAL: 配置不可读")
        return 2
    auto_mode = bool(args.auto)
    if auto_mode and (cfg.get("watchdog") or {}).get("auto_apply"):
        args.apply = True
    now = C.now_utc()
    token = os.getenv("ENGINE4_REPAIR_TOKEN") or os.getenv("ENGINE4_READ_TOKEN", "")
    if not token:
        print("SKIP: 缺少 ENGINE4_REPAIR_TOKEN")
        return 0

    resolved = resolve_pending(args.root, cfg, token, now)
    if args.resolve_only:
        print(json.dumps({"at": C.iso(now), "phase": "resolve_only", "resolved": resolved},
                         ensure_ascii=False, indent=2))
        from ops import ghcommit
        for note in ghcommit.commit_files_batch(args.root, cfg, [HISTORY],
                                                "ops: watchdog resolve run result"):
            print(note)
        if not os.path.exists(os.path.join(args.root, PENDING)):
            for note in ghcommit.delete_file(args.root, cfg, PENDING,
                                             "ops: watchdog clear resolved pending"):
                print(note)
        return 0
    ctx = gather(cfg, token, args.root, now)
    manual = bool(not auto_mode and args.apply and args.confirm == "YES-I-APPROVE")
    decision = decide({**ctx, "cfg": cfg.get("watchdog") or {}, "now": now, "manual": manual})

    report = {"at": C.iso(now), "phase": "decide", "apply": args.apply,
              "engine_runs_error": ctx["runs_error"],
              "active_runs": len(ctx["active_runs"]),
              "missed_points": ctx["missed"],
              "engine_status": ctx["engine_status"], "fresh_status": ctx["fresh_status"],
              "site_sync_status": ctx["site_sync_status"],
              "in_sale_window": ctx["in_sale_window"],
              "last_auto_age_min": ctx["last_auto_age_min"],
              "today_auto_count": ctx["today_auto_count"],
              "decision": decision, "resolved": resolved}
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not args.apply:
        return 0
    if auto_mode:
        if not (cfg.get("watchdog") or {}).get("auto_apply"):
            print("REFUSED: 自动补跑未启用（watchdog.auto_apply=false）")
            return 1
    else:
        if args.confirm != "YES-I-APPROVE":
            print("REFUSED: 缺少授权串 YES-I-APPROVE")
            return 1
        if not (cfg.get("watchdog", {}).get("auto_apply")
                or os.getenv("WATCHDOG_MANUAL_APPLY") == "1"):
            print("REFUSED: 自动补跑未启用（watchdog.auto_apply=false），且未提供人工授权通道")
            return 1
    if not decision["should_trigger"]:
        print("REFUSED: 未满足补跑条件：" + "；".join(decision["blocked_by"]))
        return 1

    repo = cfg["engine_repo"]
    wf = (cfg.get("engine") or {}).get("workflow_file", "engine4.yml")
    code, body = _api(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches",
                      token, "POST", {"ref": "main"})
    ok = code in (201, 204)
    record = {"at": C.iso(now), "phase": "dispatched", "run_id": None,
              "trigger_reason": "missed_schedule_points", "missed_points": ctx["missed"],
              "http": code, "ok": ok, "in_sale_window": ctx["in_sale_window"]}
    if ok:
        import time
        time.sleep(8)
        rc, docs = _api(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?per_page=3", token)
        if rc == 200 and docs.get("workflow_runs"):
            newest = docs["workflow_runs"][0]
            record["run_id"] = newest["id"]
            C.dump_json(os.path.join(args.root, PENDING), {
                "run_id": newest["id"], "reason": "missed_schedule_points",
                "missed_points": ctx["missed"], "at": C.iso(now)})
    append_history(args.root, record)
    from ops import ghcommit
    for note in ghcommit.commit_files_batch(args.root, cfg,
                                            [HISTORY] + ([PENDING] if ok else []),
                                            "ops: watchdog dispatch engine4"):
        print(note)
    print(json.dumps(record, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
