"""Read-only health checks for 90scale 4.0.

Every check is a pure function of a context dict and returns a result record.
No check writes to production, the website repo, Supabase, or the engine repo.
"""
from __future__ import annotations

import json
import time

from ops import common as C
from ops.common import BJ, age_min, as_json, git_blob_sha, http_get, parse_ts, result

API = "https://api.github.com"


# ------------------------------------------------------------------ utilities
def _site_url(ctx, path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{ctx['cfg']['site']}{path}{sep}cb={int(time.time() * 1000)}"


def _api(ctx, path: str, token: str | None = None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return http_get(API + path, timeout=20, retries=1, headers=headers)


def _repo_file_sha(ctx, path: str):
    res = _api(ctx, f"/repos/{ctx['cfg']['site_repo']}/contents/{path}?ref=main")
    if res["status"] != 200:
        return None, res.get("error") or f"HTTP {res['status']}"
    doc = as_json(res)
    if not isinstance(doc, dict):
        return None, "响应非 JSON"
    return doc.get("sha"), None


def _last_commit_age_min(ctx, path: str):
    res = _api(ctx, f"/repos/{ctx['cfg']['site_repo']}/commits?path={path}&per_page=1")
    doc = as_json(res)
    if res["status"] != 200 or not isinstance(doc, list) or not doc:
        return None
    ts = parse_ts(doc[0].get("commit", {}).get("committer", {}).get("date"))
    return age_min(ts, ctx["now"])


def _probe_url(template: str, ctx) -> str:
    bj = ctx["now"].astimezone(BJ)
    return (template.replace("{today}", bj.strftime("%Y-%m-%d"))
            .replace("{today_compact}", bj.strftime("%Y%m%d")))


# --------------------------------------------------------------------- checks
def check_site_home(ctx) -> dict:
    th = ctx["cfg"]["thresholds"]
    res = http_get(_site_url(ctx, "/"), timeout=25, retries=2)
    if res["status"] != 200:
        return result("site_home", "正式网站首页可访问", "fail", "critical",
                      f"首页返回 {res['status']} ({res.get('error')})")
    body = res["body"]
    missing = [m for m in (b"vip-code", b"./data/engine4/") if m not in body]
    if missing:
        return result("site_home", "正式网站首页可访问", "fail", "critical",
                      "首页缺少关键标记：" + ", ".join(m.decode() for m in missing))
    if res["bytes"] < th["site_min_bytes"]:
        return result("site_home", "正式网站首页可访问", "warn", "warn",
                      f"首页体积异常偏小：{res['bytes']} 字节")
    return result("site_home", "正式网站首页可访问", "ok", "info",
                  f"HTTP 200，{res['bytes']} 字节，耗时 {res['elapsed_ms']}ms")


def check_site_data_sync(ctx) -> dict:
    """Compare the file Pages actually serves with the blob committed to the repo."""
    res = http_get(_site_url(ctx, "/data/engine4/latest.json"), timeout=25, retries=1)
    if res["status"] != 200:
        return result("site_data_sync", "网站数据与仓库一致", "fail", "critical",
                      f"生产 latest.json 返回 {res['status']} ({res.get('error')})")
    prod_sha = git_blob_sha(res["body"])
    repo_sha, err = _repo_file_sha(ctx, "data/engine4/latest.json")
    if repo_sha is None:
        return result("site_data_sync", "网站数据与仓库一致", "skip", "info",
                      f"仓库 API 不可用，未能比对：{err}")
    extra = {"prod_bytes": res["bytes"], "prod_sha": prod_sha[:10], "repo_sha": repo_sha[:10]}
    if prod_sha == repo_sha:
        return result("site_data_sync", "网站数据与仓库一致", "ok", "info",
                      f"生产与仓库一致（{res['bytes']} 字节），Pages 已生效", **extra)
    lag = _last_commit_age_min(ctx, "data/engine4/latest.json")
    th = ctx["cfg"]["thresholds"]
    if lag is not None and lag < th["sync_lag_warn_min"]:
        return result("site_data_sync", "网站数据与仓库一致", "warn", "warn",
                      f"生产与仓库内容不一致，但仓库刚在 {lag} 分钟内提交，疑似 Pages 部署中", lag_min=lag, **extra)
    if lag is not None and lag >= th["sync_lag_fail_min"]:
        return result("site_data_sync", "网站数据与仓库一致", "fail", "critical",
                      f"生产与仓库内容不一致已 {lag} 分钟，按阈值 {th['sync_lag_fail_min']} 分钟判为同步异常", lag_min=lag, **extra)
    return result("site_data_sync", "网站数据与仓库一致", "warn", "warn",
                  f"生产与仓库内容不一致（最后提交 {lag} 分钟前），疑似部署延迟", lag_min=lag, **extra)


def check_engine_fresh(ctx) -> dict:
    """Did the prediction engine publish a snapshot for the current sale day?"""
    th = ctx["cfg"]["thresholds"]
    res = http_get(_site_url(ctx, "/data/engine4/latest.json"), timeout=25, retries=1)
    doc = as_json(res)
    if not isinstance(doc, dict):
        return result("engine_fresh", "预测主引擎当日快照", "fail", "critical",
                      f"无法读取 latest.json（HTTP {res['status']}）")
    sale_day = str(doc.get("sale_day") or "")
    generated = parse_ts(doc.get("generated_at"))
    age = age_min(generated, ctx["now"])
    today = ctx["now"].astimezone(BJ).strftime("%Y-%m-%d")
    window = C.in_sale_window(ctx["now"])
    info = {"sale_day": sale_day, "generated_age_min": age, "in_window": window}
    if window and sale_day != today:
        return result("engine_fresh", "预测主引擎当日快照", "fail", "critical",
                      f"处于销售窗口但最新 sale_day={sale_day or '空'}，不是今天 {today}：疑似漏跑", **info)
    limit_warn = th["engine_stale_warn_min"] if window else th["engine_offwindow_warn_h"] * 60
    limit_fail = th["engine_stale_fail_min"] if window else th["engine_offwindow_fail_h"] * 60
    if age is None:
        return result("engine_fresh", "预测主引擎当日快照", "warn", "warn", "快照缺少 generated_at", **info)
    if age >= limit_fail:
        return result("engine_fresh", "预测主引擎当日快照", "fail", "critical",
                      f"最新快照已 {age:.0f} 分钟未更新（阈值 {limit_fail} 分钟），疑似引擎停摆", **info)
    if age >= limit_warn:
        return result("engine_fresh", "预测主引擎当日快照", "warn", "warn",
                      f"最新快照已 {age:.0f} 分钟未更新（阈值 {limit_warn} 分钟）", **info)
    return result("engine_fresh", "预测主引擎当日快照", "ok", "info",
                  f"sale_day={sale_day}，快照 {age:.0f} 分钟前生成", **info)


def check_engine_runs(ctx) -> dict:
    """GitHub Actions 层：私人预测引擎的计划点是否漏跑（只读）。

    旧结算/核验/回填工作流已主动停用，正式赛果以 Supabase 为准，
    本检查不把它们计入，也不会触发或恢复它们。
    """
    import datetime as dt
    import os

    eng = ctx["cfg"].get("engine") or {}
    if not eng.get("enabled", True):
        return result("engine_runs", "预测引擎计划点漏跑", "skip", "info", "未启用")
    token = os.getenv("ENGINE4_READ_TOKEN") or os.getenv("ENGINE_READ_TOKEN") or ""
    if not token:
        return result("engine_runs", "预测引擎计划点漏跑", "skip", "info",
                      "未配置 ENGINE4_READ_TOKEN Secret，无法读取私人引擎运行记录")
    repo = ctx["cfg"]["engine_repo"]
    wf = eng.get("workflow_file", "engine4.yml")
    res = _api(ctx, f"/repos/{repo}/actions/workflows/{wf}/runs?per_page=30", token=token)
    if res["status"] != 200:
        return result("engine_runs", "预测引擎计划点漏跑", "skip", "info",
                      f"运行记录不可读（HTTP {res['status']}）")
    doc = as_json(res) or {}
    runs = doc.get("workflow_runs") or []
    now = ctx["now"]
    bj_now = now.astimezone(BJ)

    created = []
    for r in runs:
        ts = parse_ts(r.get("created_at"))
        if ts:
            created.append((ts, r.get("event"), r.get("conclusion")))

    grace = dt.timedelta(minutes=float(eng.get("grace_min", 20)))
    lookback = dt.timedelta(minutes=float(eng.get("lookback_min", 180)))
    missed, checked = [], 0
    for hhmm in eng.get("schedule_bj", []):
        hh, mm = (int(x) for x in hhmm.split(":"))
        point = bj_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if point > bj_now or (bj_now - point) > lookback:
            continue
        checked += 1
        if not any(point - dt.timedelta(minutes=2) <= ts <= point + grace for ts, _, _ in created):
            missed.append(hhmm)

    today = [c for c in created if c[0].astimezone(BJ).date() == bj_now.date()]
    last_age = age_min(max((c[0] for c in created), default=None), now)
    info = {"missed_points": missed, "checked_points": checked,
            "runs_today": len(today), "last_run_age_min": last_age,
            "retired_workflows_ignored": eng.get("retired_workflows", [])}

    if bj_now.hour >= 10 and not today:
        return result("engine_runs", "预测引擎计划点漏跑", "fail", "critical",
                      "北京时间 10 点后引擎今天仍无任何运行记录", **info)
    if missed:
        return result("engine_runs", "预测引擎计划点漏跑", "warn", "warn",
                      f"近 {int(lookback.total_seconds() // 60)} 分钟内 {len(missed)} 个计划点无运行："
                      f"{'、'.join(missed)}；最近一次运行 {last_age:.0f} 分钟前", **info)
    return result("engine_runs", "预测引擎计划点漏跑", "ok", "info",
                  f"已核对 {checked} 个计划点，无漏跑；今天 {len(today)} 次运行", **info)


def check_pool_coverage(ctx) -> dict:
    res = http_get(_site_url(ctx, "/data/engine4/latest.json"), timeout=25, retries=1)
    doc = as_json(res)
    if not isinstance(doc, dict):
        return result("pool_coverage", "当日赛事池完整性", "skip", "info", "latest.json 不可读")
    matches = doc.get("matches") or []
    metrics = doc.get("metrics") or {}
    total = metrics.get("total")
    window = C.in_sale_window(ctx["now"])
    no_pred = [m.get("match_no") for m in matches
               if not (isinstance(m.get("prediction"), dict) and m["prediction"])]
    info = {"matches": len(matches), "metrics_total": total, "missing_prediction": len(no_pred)}
    if window and not matches:
        return result("pool_coverage", "当日赛事池完整性", "fail", "critical",
                      "销售窗口内当日赛事池为空", **info)
    if total is not None and int(total) != len(matches):
        return result("pool_coverage", "当日赛事池完整性", "warn", "warn",
                      f"metrics.total={total} 与实际场次 {len(matches)} 不一致", **info)
    if no_pred:
        return result("pool_coverage", "当日赛事池完整性", "warn", "warn",
                      f"{len(no_pred)} 场缺少 prediction：{no_pred[:6]}", **info)
    return result("pool_coverage", "当日赛事池完整性", "ok", "info",
                  f"{len(matches)} 场，均含预测", **info)


def check_settlement(ctx) -> dict:
    """For already-finished sale days, are results settled?"""
    th = ctx["cfg"]["thresholds"]
    idx = as_json(http_get(_site_url(ctx, "/data/engine4/sales/index.json"), timeout=20, retries=1))
    days = (idx or {}).get("days") or []
    today = ctx["now"].astimezone(BJ).strftime("%Y-%m-%d")
    past = [d for d in days if d < today][-3:]
    if not past:
        return result("settlement", "赛果核验与结算", "skip", "info", "没有已完成销售日可核验")
    problems_fail, problems_warn, checked = [], [], []
    for day in past:
        doc = as_json(http_get(_site_url(ctx, f"/data/engine4/sales/{day}.json"), timeout=25, retries=1))
        if not isinstance(doc, dict):
            problems_fail.append(f"{day} 快照不可读")
            continue
        matches = doc.get("matches") or []
        metrics = doc.get("metrics") or {}
        pending = metrics.get("pending")
        if pending is None:
            pending = sum(1 for m in matches if not m.get("settled") and not m.get("result"))
        oldest = None
        for m in matches:
            if m.get("settled") or m.get("result"):
                continue
            ts = parse_ts(m.get("kickoff"))
            if ts and (oldest is None or ts < oldest):
                oldest = ts
        overdue_h = None
        if int(pending or 0) > 0 and oldest is not None:
            overdue_h = round((ctx["now"] - oldest).total_seconds() / 3600.0, 1)
        checked.append({"day": day, "matches": len(matches), "pending": int(pending or 0),
                        "overdue_h": overdue_h})
        if int(pending or 0) > 0 and overdue_h is not None:
            if overdue_h >= th["settlement_overdue_fail_h"]:
                problems_fail.append(f"{day} 有 {pending} 场开赛 {overdue_h}h 仍未结算")
            elif overdue_h >= th["settlement_overdue_warn_h"]:
                problems_warn.append(f"{day} 有 {pending} 场开赛 {overdue_h}h 未结算（观察中）")
    info = {"checked": checked}
    if problems_fail:
        return result("settlement", "赛果核验与结算", "fail", "critical", "；".join(problems_fail), **info)
    if problems_warn:
        return result("settlement", "赛果核验与结算", "warn", "warn", "；".join(problems_warn), **info)
    latest = checked[-1] if checked else {}
    return result("settlement", "赛果核验与结算", "ok", "info",
                  f"已核验 {len(checked)} 个销售日，无超时未结算（最近 {latest.get('day')}）", **info)


def check_independent_review(ctx) -> dict:
    th = ctx["cfg"]["thresholds"]
    res = _api(ctx, f"/repos/{ctx['cfg']['site_repo']}/contents/data/independent-review?ref=main")
    if res["status"] != 200:
        return result("independent_review", "独立复核数据新鲜度", "skip", "info",
                      f"目录不可读（HTTP {res['status']}）")
    files = as_json(res) or []
    dates = sorted({f.get("name", "")[:10] for f in files if f.get("name", "").endswith(".json")})
    if not dates:
        return result("independent_review", "独立复核数据新鲜度", "warn", "warn", "目录内没有复核文件")
    latest = dates[-1]
    try:
        import datetime as dt
        d = dt.date.fromisoformat(latest)
        lag_days = (ctx["now"].astimezone(BJ).date() - d).days
    except ValueError:
        return result("independent_review", "独立复核数据新鲜度", "warn", "warn", f"文件名日期异常：{latest}")
    if lag_days >= th["review_stale_fail_d"]:
        return result("independent_review", "独立复核数据新鲜度", "fail", "warn",
                      f"最新复核文件 {latest}，已滞后 {lag_days} 天", latest=latest, lag_days=lag_days)
    if lag_days >= th["review_stale_warn_d"]:
        return result("independent_review", "独立复核数据新鲜度", "warn", "warn",
                      f"最新复核文件 {latest}，已滞后 {lag_days} 天", latest=latest, lag_days=lag_days)
    return result("independent_review", "独立复核数据新鲜度", "ok", "info",
                  f"最新复核文件 {latest}（滞后 {lag_days} 天）", latest=latest, lag_days=lag_days)


def check_sources(ctx) -> dict:
    """Probe every upstream collector chain; record reachability and latency.

    中国站点在 GitHub 境外 runner 上常被拒绝（403/超时），这不代表站点故障。
    规则：境外 runner + 仅 CN 专属源失败 + 至少还有一个 CN 源可达 → 判 skip（不触发 AI），
    只有所有 CN 源都不可达时才判 fail（那说明网络路径或上游真的断了）。
    """
    import os

    th = ctx["cfg"]["thresholds"]
    rows, down_required, down_optional = [], [], []
    for p in ctx["cfg"].get("probes", []):
        if not p.get("enabled", True) or not p.get("url"):
            rows.append({"id": p["id"], "name": p["name"], "status": "disabled",
                         "note": p.get("note", "")})
            continue
        res = http_get(_probe_url(p["url"], ctx), timeout=th["probe_timeout_s"], retries=1,
                       headers=p.get("headers"))
        ok = res["status"] == 200 and res["bytes"] >= int(p.get("min_bytes", 1))
        row = {"id": p["id"], "name": p["name"], "status": "up" if ok else "down",
               "http": res["status"], "bytes": res["bytes"], "elapsed_ms": res["elapsed_ms"],
               "error": res.get("error"), "cn_only": bool(p.get("cn_only")),
               "required": bool(p.get("required"))}
        rows.append(row)
        if not ok:
            (down_required if p.get("required") else down_optional).append(p["name"])

    info = {"probes": rows}
    failed_rows = [r for r in rows if r["status"] == "down"]
    cn_up = [r for r in rows if r["status"] == "up" and r["cn_only"]]
    if down_required:
        failed_req = [r for r in failed_rows if r["required"]]
        offshore = os.getenv("OPS_RUNNER_REGION", "cn" if os.getenv("GITHUB_ACTIONS") != "true" else "global") == "global"
        if offshore and failed_req and all(r["cn_only"] for r in failed_req) and cn_up:
            return result("sources", "采集链路可用性", "skip", "info",
                          "境外 runner 访问中国站点被拒（预期，不计入异常）："
                          + "、".join(r["name"] for r in failed_req)
                          + f"；同网络下 {'、'.join(r['name'] for r in cn_up)} 正常", **info)
        return result("sources", "采集链路可用性", "fail", "critical",
                      "关键数据源不可用：" + "、".join(down_required), **info)
    if down_optional:
        return result("sources", "采集链路可用性", "warn", "warn",
                      "备用数据源不可用（主链路仍可用）：" + "、".join(down_optional), **info)
    if not rows:
        return result("sources", "采集链路可用性", "skip", "info", "未配置探测源")
    return result("sources", "采集链路可用性", "ok", "info",
                  f"{len(rows)} 条链路全部可达", **info)


def check_site_workflows(ctx) -> dict:
    """Recent GitHub Actions health in the website repo (read-only, public API)."""
    th = ctx["cfg"]["thresholds"]
    res = _api(ctx, f"/repos/{ctx['cfg']['site_repo']}/actions/runs?per_page=30")
    if res["status"] != 200:
        return result("site_workflows", "网站仓库工作流健康", "skip", "info",
                      f"Actions API 不可用（HTTP {res['status']}）")
    doc = as_json(res) or {}
    window = th["workflow_fail_window_min"]
    failures, recent = [], []
    for run in doc.get("workflow_runs", []):
        created = parse_ts(run.get("created_at"))
        age = age_min(created, ctx["now"])
        if age is None or age > window:
            continue
        if run.get("conclusion") == "failure":
            failures.append({"name": run.get("name"), "at": run.get("created_at"),
                             "url": run.get("html_url")})
        recent.append(run.get("name"))
    info = {"failures": failures, "recent_runs": len(recent)}
    if failures:
        names = "、".join(sorted({f["name"] for f in failures}))
        return result("site_workflows", "网站仓库工作流健康", "fail", "warn",
                      f"{window} 分钟内有 {len(failures)} 次失败：{names}", **info)
    if not recent:
        return result("site_workflows", "网站仓库工作流健康", "warn", "warn",
                      f"{window} 分钟内没有任何工作流运行记录，疑似调度停滞", **info)
    return result("site_workflows", "网站仓库工作流健康", "ok", "info",
                  f"{window} 分钟内 {len(recent)} 次运行，无失败", **info)


def check_legacy_sale_status(ctx) -> dict:
    """sale-day-status.json is legacy; report staleness as info, never as an incident."""
    doc = as_json(http_get(_site_url(ctx, "/data/engine4/sale-day-status.json"), timeout=15, retries=1))
    if not isinstance(doc, dict):
        return result("legacy_sale_status", "旧版销售日状态文件", "skip", "info", "文件不可读")
    ts = parse_ts(doc.get("checked_at"))
    age_d = round((age_min(ts, ctx["now"]) or 0) / 1440.0, 1)
    return result("legacy_sale_status", "旧版销售日状态文件", "ok", "info",
                  f"最后一次更新 {age_d} 天前，状态 {doc.get('status')}；首页未引用，仅作记录",
                  sale_status=doc.get("status"), age_days=age_d)


CHECKS = [
    check_site_home,
    check_site_data_sync,
    check_engine_fresh,
    check_engine_runs,
    check_pool_coverage,
    check_settlement,
    check_independent_review,
    check_sources,
    check_site_workflows,
    check_legacy_sale_status,
]


def run_all(cfg: dict, now=None, only: list[str] | None = None) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    now = now or C.now_utc()
    ctx = {"cfg": cfg, "now": now}
    fns = [f for f in CHECKS if not only or f.__name__.replace("check_", "") in only]
    out = []
    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = {pool.submit(fn, ctx): fn for fn in fns}
        for fut in futures:
            fn = futures[fut]
            try:
                out.append(fut.result())
            except Exception as e:  # noqa: BLE001 - a broken check must not kill the run
                out.append(result(fn.__name__.replace("check_", ""), fn.__doc__ or fn.__name__,
                                  "fail", "warn", f"检查自身异常：{type(e).__name__}: {e}"))
    order = {f.__name__.replace("check_", ""): i for i, f in enumerate(CHECKS)}
    out.sort(key=lambda c: order.get(c["id"], 99))
    return out
