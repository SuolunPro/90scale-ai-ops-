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


def build_markdown(doc: dict) -> str:
    lines = [
        f"## 九十刻度 AI 运维巡检 — {doc['verdict']}",
        "",
        f"- 时间：{doc['generated_at']}",
        f"- 销售窗口内：{'是' if doc['in_sale_window'] else '否'}",
        f"- 失败项：{', '.join(doc['failed']) or '无'}",
        f"- 预警项：{', '.join(doc['warned']) or '无'}",
        f"- 本月 AI 预算：已用 {doc['budget']['spent_cny']:.4f} / {doc['budget']['cap_cny']:.2f} 元"
        f"（剩余 {doc['budget']['remaining_cny']:.4f} 元，调用 {doc['budget']['calls']} 次）",
        "",
        "| 状态 | 检查 | 说明 |",
        "| --- | --- | --- |",
    ]
    label = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    for c in doc["checks"]:
        detail = str(c.get("detail", "")).replace("|", "\\|")
        lines.append(f"| {label.get(c['status'], c['status'])} | {c['title']} | {detail} |")
    probes = next((c for c in doc["checks"] if c["id"] == "sources"), None)
    if probes and probes.get("probes"):
        lines += ["", "### 采集链路", "", "| 渠道 | 状态 | HTTP | 体积 | 耗时 |", "| --- | --- | --- | --- | --- |"]
        for p in probes["probes"]:
            lines.append(
                f"| {p.get('name')} | {p.get('status')} | {p.get('http', '-')} | "
                f"{p.get('bytes', '-')} | {p.get('elapsed_ms', '-')}ms |")
    return "\n".join(lines) + "\n"


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

    now = C.now_utc()
    only = [s for s in args.only.split(",") if s] or None
    results = checks.run_all(cfg, now=now, only=only)
    verdict = C.verdict_of(results)
    failed = [c["id"] for c in results if c["status"] == "fail"]
    warned = [c["id"] for c in results if c["status"] == "warn"]

    ledger = budget.load_ledger(args.root, now)
    bsum = budget.summary(ledger, cfg["budget"]["cap_cny"])

    doc = {
        "schema": 1,
        "generated_at": C.iso(now),
        "beijing_time": now.astimezone(C.BJ).strftime("%Y-%m-%d %H:%M:%S"),
        "in_sale_window": C.in_sale_window(now),
        "verdict": verdict,
        "signature": ",".join(sorted(failed)) or ",".join(sorted(warned)) or "OK",
        "failed": failed,
        "warned": warned,
        "checks": results,
        "budget": bsum,
        "runner": {
            "host": platform.node(),
            "python": platform.python_version(),
            "ci": os.getenv("GITHUB_ACTIONS") == "true",
            "run_id": os.getenv("GITHUB_RUN_ID", ""),
            "repo": os.getenv("GITHUB_REPOSITORY", ""),
        },
    }

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
