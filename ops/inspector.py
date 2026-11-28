"""Run all read-only checks and emit a machine-readable status document.

Usage:
    python ops/inspector.py --config config/ops.json --out state/latest.json \
        --summary "$GITHUB_STEP_SUMMARY"

Note: the module is named inspector (not inspect) on purpose - a file named
inspect.py inside ops/ would shadow the Python standard library module.
"""
from __future__ import annotations

import argparse
import os
import platform
import sys

if __package__ in (None, ""):  # allow `python ops/inspector.py` from the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import budget, checks  # noqa: E402
from ops import common as C  # noqa: E402


def beijing(value):
    date = C.parse_ts(value)
    return date.astimezone(C.BJ).strftime("%Y-%m-%d %H:%M:%S") if date else "未知"


def build_markdown(doc: dict) -> str:
    lines = [
        f"# 九十刻度4.0 AI运维日报｜{doc.get('beijing_time') or beijing(doc['generated_at'])} 北京时间",
        "",
        f"**{doc['verdict']}** · 当前故障 {'、'.join(doc['failed']) or '无明确故障'} · 风险 {'、'.join(doc['warned']) or '无'}",
        "",
        "| 项目 | 状态 | 最新证据时间（北京时间） | 结果 |",
        "| --- | --- | --- | --- |",
    ]
    for check in doc["checks"]:
        detail = str(check["detail"]).replace("|", "\\|")
        when = beijing(check.get("receipt_at") or check.get("checked_at") or check.get("generated_at"))
        link = f" [证据]({check['link']})" if check.get("link") else ""
        lines.append(f"| {check['title']} | {check['status']} | {when} | {detail}{link} |")

    for check in doc["checks"]:
        if check.get("coverage"):
            lines += ["", "## 逐场覆盖", "", "| 字段 | 可用 / 总数 | 缺失 | 陈旧 | 身份冲突 | 待核验或无效 |",
                      "| --- | --- | --- | --- | --- | --- |"]
            for name, bucket in check["coverage"].items():
                hits = len(bucket.get("available", []))
                total = sum(map(len, bucket.values()))
                unverified = len(bucket.get("unverified", [])) + len(bucket.get("invalid", []))
                lines.append(f"| {name} | {hits}/{total} ({hits/total:.1%}) | {len(bucket.get('missing', []))} | "
                             f"{len(bucket.get('stale', []))} | {len(bucket.get('match_failed', []))} | {unverified} |")
            bf = check["coverage"].get("必发指数", {})
            labels = {"missing": "缺失", "unverified": "待核验", "invalid": "无效", "stale": "陈旧"}
            gaps = [f"{label} {'、'.join(bf[name])}" for name, label in labels.items() if bf.get(name)]
            if gaps:
                lines += ["", "必发赛号（销售日 / 编号）", "", *["- " + gap for gap in gaps]]
            market = check.get("betfair_breakdown") or {}
            for key, label in (("frozen_missing", "已停售缺失"), ("eligible_missing", "已核实在售待补")):
                if market.get(key):
                    lines += ["", f"- {label} · {'、'.join(market[key])}"]
            for name, numbers in market.get("publication_issues", {}).items():
                lines += ["", f"- 发布阶段必发字段丢失 · {name} · {'、'.join(numbers)}"]
        if check.get("probes"):
            lines += ["", "## 上游端点探测", ""]
            lines += [f"- {p['name']} · HTTP {p['http']} · {p['elapsed_ms']} ms" for p in check["probes"]]

    recovery = next((c.get("recovery") for c in doc["checks"] if c["id"] == "self_heal"), None)
    if recovery:
        for key, label in (("recovered", "已取得发布证据"), ("unverified", "仍待复验")):
            for item in recovery.get(key, []):
                run_id = item["audit_id"]
                runs_url = f"https://github.com/SuolunPro/suolunpro.github.io/actions/runs/{run_id}"
                lines += ["", f"- {label} · 回执 {run_id} · 目前正式站快照 {beijing(item['published_at'])}。 [公开审计]({runs_url})"]
    ledger = doc["budget"]
    if ledger.get("exists"):
        lines += ["", f"DeepSeek 账本估算 · 当日 ¥{ledger['today_cny']:.6f}（{ledger['today_calls']} 次）"
                  f" · 当月 ¥{ledger['spent_cny']:.6f}（{ledger['calls']} 次）"
                  f" · 内部预算 ¥{ledger['cap_cny']:.2f}。账本记录截至 {beijing(ledger.get('updated_at'))}。"]
    else:
        lines += ["", "DeepSeek 账本估算 · 当前账本不可读，费用待核实。"]
    return "\n".join(lines) + "\n"


def build_status(cfg, now, root, only=None, current_ledger=None):
    results = checks.run_all(cfg, now=now, only=only, root=root)
    failed = [c["id"] for c in results if c["status"] == "FAIL"]
    warned = [c["id"] for c in results if c["status"] == "WARN"]
    ledger = current_ledger if current_ledger is not None else C.load_json(budget.ledger_path(root, now))
    entries = (ledger or {}).get("entries", [])
    today = [e for e in entries if (ts := C.parse_ts(e.get("at")))
             and ts.astimezone(C.BJ).date() == now.astimezone(C.BJ).date()]
    estimate = budget.summary(ledger or budget.load_ledger(root, now), cfg["budget"]["cap_cny"])
    estimate.update(exists=isinstance(ledger, dict), today_calls=len(today),
                    today_cny=budget.spent({"entries": today}),
                    updated_at=max((e.get("at") for e in entries if e.get("at")), default=None))
    sale = next((c.get("sale_state") for c in results if c["id"] == "engine_fresh"), None)
    return {
        "schema": 2, "generated_at": C.iso(now),
        "beijing_time": beijing(C.iso(now)), "production_repo": cfg["site_repo"],
        "sale_state": sale or "UNKNOWN", "in_sale_window": {"OPEN": True, "CLOSED": False}.get(sale),
        "verdict": C.verdict_of(results),
        "fingerprint": ",".join(f"{c['id']}={c['status']}" for c in results),
        "signature": ",".join(sorted(failed)) or ",".join(sorted(warned)) or "PASS",
        "failed": failed, "warned": warned, "checks": results, "budget": estimate,
        "runner": {"host": platform.node(), "python": platform.python_version(),
                   "ci": os.getenv("GITHUB_ACTIONS") == "true", "run_id": os.getenv("GITHUB_RUN_ID", ""),
                   "repo": os.getenv("GITHUB_REPOSITORY", "")},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--out", default="state/latest.json")
    ap.add_argument("--summary", default="")
    ap.add_argument("--only", default="", help="comma separated check ids")
    ap.add_argument("--root", default=".")
    ap.add_argument("--strict", action="store_true", help="exit 1 when verdict is FAIL")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    if not cfg:
        print(f"FATAL: cannot read config {args.config}", file=sys.stderr)
        return 2

    only = [s for s in args.only.split(",") if s] or None
    doc = build_status(cfg, C.now_utc(), args.root, only)
    verdict = doc["verdict"]
    failed = doc["failed"]

    C.dump_json(os.path.join(args.root, args.out), doc)
    md = build_markdown(doc)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(md)
    if not args.quiet:
        print(md)

    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"verdict={verdict}\n")
            f.write(f"failed={','.join(failed)}\n")
            f.write(f"signature={doc['signature']}\n")

    return 1 if (args.strict and verdict == "FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
