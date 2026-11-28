"""云端只读工单执行器。

在 GitHub Actions 里扫描 tasks/*.json，领取 status=queued 且 scope=read-only 的工单，
执行**白名单内的只读动作**，然后把结论写入 reports/、把工单置为 done/failed。

安全设计
--------
* 任务只能调用 ACTIONS 里登记的函数，`params` 会做类型/长度校验，禁止任意命令。
* 只接受 scope=read-only；写生产、改算法、改历史数据的工单直接拒绝。
* 每个工单只能被领取一次（claim 状态机），避免重复执行。
* 单次最多执行 max_tasks_per_run 个工单，超时即停，防止拖垮 runner。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import checks, ghcommit, handoff  # noqa: E402
from ops import common as C  # noqa: E402

ACTIONS: dict = {}


def action(fn):
    ACTIONS[fn.__name__.replace("action_", "")] = fn
    return fn


def _str(params: dict, key: str, default: str = "", maxlen: int = 300) -> str:
    v = str(params.get(key, default) or "")[:maxlen]
    if any(ch in v for ch in "\n\r\t"):
        raise ValueError(f"参数 {key} 含非法字符")
    return v


@action
def site_probe(cfg, params, now):
    url = _str(params, "url", cfg["site"] + "/")
    res = C.http_get(url, timeout=25, retries=1)
    body = res.get("body") or b""
    data = {"url": url, "status": res["status"], "bytes": res["bytes"],
            "elapsed_ms": res["elapsed_ms"], "error": res.get("error"),
            "checked_at": C.iso(now)}
    if url.endswith(".json") and res["status"] == 200:
        doc = C.as_json(res)
        if isinstance(doc, dict):
            data["sale_day"] = doc.get("sale_day")
            data["generated_at"] = doc.get("generated_at")
            data["matches"] = len(doc.get("matches") or [])
    elif res["status"] == 200:
        data["has_vip_marker"] = b"vip-code" in body
        data["has_engine4"] = b"./data/engine4/" in body
    ok = res["status"] == 200 and res["bytes"] > 0
    return {"ok": ok, "summary": f"{url} 返回 {res['status']}，{res['bytes']} 字节，{res['elapsed_ms']}ms",
            "data": data}


@action
def inspect_all(cfg, params, now):
    results = checks.run_all(cfg, now=now)
    verdict = C.verdict_of(results)
    bad = [{"id": c["id"], "status": c["status"], "detail": c["detail"]}
           for c in results if c["status"] in ("FAIL", "WARN")]
    return {"ok": verdict not in ("FAIL", "UNKNOWN"),
            "summary": f"全量巡检判定 {verdict}，异常 {len(bad)} 项",
            "data": {"verdict": verdict, "abnormal": bad,
                     "checks": {c["id"]: c["status"] for c in results}}}


@action
def engine_runs_audit(cfg, params, now):
    result, = checks.run_all(cfg, now=now, only=["engine_runs"])
    return {"ok": result["status"] not in ("FAIL", "UNKNOWN"),
            "summary": result["detail"], "data": result}


@action
def workflow_runs(cfg, params, now):
    import urllib.request

    repo = _str(params, "repo", cfg["site_repo"])
    if repo not in (cfg["site_repo"], cfg["ops_repo"]):
        return {"ok": False, "summary": "请使用公开生产仓库；私人仓库运行仅由额度巡检审计。", "data": {}}
    wf = _str(params, "workflow_file", "")
    n = int(params.get("limit", 10))
    path = (f"/repos/{repo}/actions/workflows/{wf}/runs?per_page={n}" if wf
            else f"/repos/{repo}/actions/runs?per_page={n}")
    req = urllib.request.Request("https://api.github.com" + path,
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": C.UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"读取 {repo} 运行记录失败：{type(e).__name__}",
                "data": {"error": str(e)[:200]}}
    rows = [{"at": x["created_at"], "event": x["event"], "status": x["status"],
             "conclusion": x["conclusion"], "name": x["name"]}
            for x in doc.get("workflow_runs", [])]
    return {"ok": True, "summary": f"{repo} 最近 {len(rows)} 次运行", "data": {"runs": rows}}


def validate_spec(doc: dict) -> tuple[bool, str]:
    if doc.get("scope") != "read-only":
        return False, "第一阶段只允许 read-only 工单"
    act = doc.get("action")
    if not act:
        return False, "工单缺少 action 字段"
    if act not in ACTIONS:
        return False, f"未知 action：{act}（白名单：{sorted(ACTIONS)}）"
    if not isinstance(doc.get("params", {}), dict):
        return False, "params 必须是对象"
    return True, "ok"


def run_task(root: str, cfg: dict, doc: dict, now) -> dict:
    act = doc["action"]
    started = time.time()
    try:
        out = ACTIONS[act](cfg, doc.get("params") or {}, now)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "summary": f"执行异常：{type(e).__name__}: {e}", "data": {}}
    elapsed = round(time.time() - started, 2)
    verdict = "OK" if out["ok"] else "FAIL"
    findings = [out["summary"]] + [f"{k}={v}" for k, v in list(out["data"].items())[:8]
                                   if not isinstance(v, (dict, list))]
    report = handoff.complete(
        root, doc["id"], verdict, out["summary"],
        findings=findings,
        evidence=[f"task={doc['id']}", f"action={act}", f"elapsed_s={elapsed}",
                  f"raw={json.dumps(out['data'], ensure_ascii=False)[:800]}"],
        next_actions=["人工复核报告", "如为 FAIL，按 RUNBOOK 处理并保留证据"],
        author="deepseek-executor", now=now)
    return {"task": doc["id"], "verdict": verdict, "summary": out["summary"],
            "elapsed_s": elapsed, "report": report["report_id"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--requeue", default="", help="把失败工单重新排队（受 max_attempts 限制）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    if not cfg:
        print("FATAL: 配置不可读")
        return 2
    now = C.now_utc()

    if args.requeue:
        doc = handoff.load_task(args.root, args.requeue)
        max_attempts = int((cfg.get("executor") or {}).get("max_attempts", 2))
        attempts = int(doc.get("attempts", 0))
        if attempts >= max_attempts:
            print(f"REFUSED: {args.requeue} 已执行 {attempts} 次，达到上限 {max_attempts}，需人工介入")
            return 1
        doc.update({"status": "queued", "attempts": attempts + 1, "requeued_at": C.iso(now),
                    "assignee": "", "report_id": doc.get("report_id")})
        handoff.save_task(args.root, doc)
        ghcommit.commit_files_batch(args.root, cfg, [f"tasks/{doc['id']}.json"],
                                    f"ops: requeue {doc['id']} (attempt {attempts + 1})")
        print(f"requeued {doc['id']} attempt {attempts + 1}/{max_attempts}")
        return 0

    limit = int((cfg.get("executor") or {}).get("max_tasks_per_run", 3))

    queued = [t for t in handoff.list_tasks(args.root, "queued")]
    if args.task_id:
        queued = [t for t in queued if t["id"] == args.task_id]
    print(f"待执行工单：{len(queued)}")
    done, skipped = [], []
    for doc in queued[:limit]:
        ok, why = validate_spec(doc)
        if not ok:
            skipped.append((doc["id"], why))
            print(f"  SKIP {doc['id']}: {why}")
            continue
        if args.dry_run:
            print(f"  [dry] {doc['id']} action={doc['action']}")
            continue
        handoff.claim(args.root, doc["id"], by="deepseek-executor", now=now)
        res = run_task(args.root, cfg, doc, now)
        done.append(res)
        print(f"  DONE {res['task']} {res['verdict']} ({res['elapsed_s']}s) -> {res['report']}")

    summary = {"executed": done, "skipped": skipped, "at": C.iso(now)}
    C.dump_json(os.path.join(args.root, "state", "executor", "last_run.json"), summary)

    if done and not args.dry_run:
        files = [f"tasks/{d['task']}.json" for d in done]
        files += [f"reports/{d['report']}.json" for d in done]
        files += [f"reports/{d['report']}.md" for d in done]
        files += ["state/executor/last_run.json"]
        for note in ghcommit.commit_files_batch(args.root, cfg, files, "ops: execute read-only tasks"):
            print(note)

    print(json.dumps(summary, ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
