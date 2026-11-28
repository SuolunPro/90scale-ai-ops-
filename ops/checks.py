"""Read-only production checks and independent endpoint diagnostics."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ops import common as C
from ops import production


def _probe_url(template, ctx):
    bj = ctx["now"].astimezone(C.BJ)
    return template.replace("{today}", bj.date().isoformat()).replace("{today_compact}", bj.strftime("%Y%m%d"))


def check_endpoints(cfg, now):
    probes = [p for p in cfg["probes"] if p.get("enabled", True) and p.get("url")]
    ctx = {"now": now}
    with ThreadPoolExecutor(max_workers=5) as pool:
        answers = [pool.submit(C.http_get, _probe_url(p["url"], ctx), timeout=8, retries=0,
                               headers=p.get("headers")) for p in probes]
        rows = []
        for p, answer in zip(probes, answers):
            res = answer.result()
            rows.append({"id": p["id"], "name": p["name"], "http": res["status"],
                         "bytes": res["bytes"], "elapsed_ms": res["elapsed_ms"],
                         "status": "PASS" if res["status"] == 200 else "UNKNOWN",
                         "error": res.get("error"), "link": res["url"]})
    return production.outcome("endpoints", "上游端点可达性", "PASS" if rows and all(
        r["status"] == "PASS" for r in rows) else "UNKNOWN",
        "以下仅为巡检节点的 HTTP 响应，实际采集以逐场正式数据为准。",
        probes=rows, checked_at=C.iso(now))


def run_all(cfg, now=None, only=None, root="."):
    now = now or C.now_utc()
    with ThreadPoolExecutor(max_workers=2) as pool:
        endpoint = pool.submit(check_endpoints, cfg, now) if not only or "endpoints" in only else None
        checks = production.audit(cfg, now, root) if only != ["endpoints"] else []
        if endpoint:
            checks.append(endpoint.result())
    return [check for check in checks if not only or check["id"] in only]
