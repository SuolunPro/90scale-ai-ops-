"""Cost-gated DeepSeek triage: only runs when the inspector reports an anomaly.

Guarantees
----------
* Never calls DeepSeek without evidence of a current WARN or FAIL.
* Never exceeds the monthly CNY cap (config/ops.json -> budget.cap_cny).
* Deduplicates repeated identical failures inside a cool-down window.
* Read-only: produces diagnosis + suggested fix, never applies it.
* Persists the budget ledger back to this repository via the GitHub API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

if __package__ in (None, ""):  # allow `python ops/triage.py` from the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import budget, ghcommit  # noqa: E402
from ops import common as C  # noqa: E402

SYSTEM_PROMPT = (
    "你是九十刻度4.0只读诊断助手。正式生产为 SuolunPro/suolunpro.github.io。"
    "只用 v2 最新证据，状态仅 PASS/WARN/FAIL/UNKNOWN/SKIP/RETIRED/RECOVERED。"
    "权限不足、旧档停更、研究影子、正常停售以及延期未完赛都不属于生产故障。"
    "端点 HTTP 200 只说明可达；依据逐场快照区分缺失、陈旧、身份冲突。"
    "运行成功和派发请求仅为过程；恢复需有同销售日公开 main 与正式站产物证据。"
    "唯一补偿入口是正式生产 public-refresh-audit.yml，由其执行在售与防重闸门。"
    "严禁建议触发私人引擎、恢复旧工作流、覆盖冻结预测、改算法、会员或赛果。"
    "只输出 JSON，包含 summary、root_causes（最多3条，引用证据）、customer_impact、escalate。"
    "证据不足写 UNKNOWN。根因未确认时提出只读复核项。"
)


def diagnostic_payload(doc):
    fields = ("id", "status", "detail", "link", "checked_at", "receipt_at", "sale_state",
              "publication_status", "snapshot_issues")
    checks = [{k: c[k] for k in fields if k in c} for c in doc.get("checks", [])
              if c["status"] in {"WARN", "FAIL"}]
    return {"production_repo": doc["production_repo"], "verdict": doc["verdict"],
            "beijing_time": doc.get("beijing_time"), "sale_state": doc.get("sale_state"),
            "checks": checks}


def _post_deepseek(cfg: dict, messages: list) -> dict:
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    b = cfg["budget"]
    body = {
        "model": b.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": float(b.get("temperature", 0.2)),
        "max_tokens": int(b.get("max_output_tokens", 800)),
        "stream": False,
    }
    url = cfg.get("deepseek", {}).get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"DeepSeek HTTP {e.code}: {detail}") from None


def _parse_json_text(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")
        t = t[1] if len(t) > 1 else t[0]
        t = t.lstrip("json").strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {"raw": text}
    except ValueError:
        return {"raw": text}


def _today_entries(ledger: dict, now) -> int:
    day = now.astimezone(C.BJ).strftime("%Y-%m-%d")
    return sum(1 for e in ledger.get("entries", []) if (ts := C.parse_ts(e.get("at")))
               and ts.astimezone(C.BJ).date().isoformat() == day)


def _dedupe_hit(ledger: dict, signature: str, window_min: float, now) -> str:
    cutoff = now.timestamp() - window_min * 60
    for e in reversed(ledger.get("entries", [])):
        if e.get("note", "").endswith(":" + signature):
            try:
                from datetime import datetime
                at = datetime.fromisoformat(str(e.get("at")))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=C.BJ)
            if at.timestamp() >= cutoff:
                return f"同一故障签名 {signature} 在 {window_min:.0f} 分钟内已诊断过，跳过重复调用"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/ops.json")
    ap.add_argument("--status", default="state/latest.json")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)

    cfg = C.load_json(os.path.join(args.root, args.config))
    doc = C.load_json(os.path.join(args.root, args.status))
    if not cfg or not doc:
        print("FATAL: 缺少 config 或 status 文件", file=sys.stderr)
        return 2

    now = C.now_utc()
    verdict = doc.get("verdict", "UNKNOWN")
    signature = doc.get("signature", "UNKNOWN")
    b = cfg["budget"]

    if (doc.get("schema") != 2 or doc.get("production_repo") != cfg["site_repo"]
            or verdict not in {"FAIL", "WARN"}):
        print(f"SKIP: {verdict} 或输入缺少当前公开巡检证据")
        return 0
    generated = C.parse_ts(doc.get("generated_at"))
    if not generated or not 0 <= C.age_min(generated, now) <= 45:
        print("SKIP: 巡检证据过期或时间无效")
        return 0
    payload = diagnostic_payload(doc)
    if not payload["checks"]:
        print("SKIP: 无可证实的异常项")
        return 0

    ledger = budget.load_ledger(args.root, now)
    window = b["fail_dedupe_min"] if verdict == "FAIL" else b["warn_dedupe_min"]
    hit = _dedupe_hit(ledger, signature, float(window), now)
    if hit:
        print("SKIP: " + hit)
        return 0
    if _today_entries(ledger, now) >= int(b.get("max_calls_per_day", 12)):
        print(f"SKIP: 今日 AI 调用已达上限 {b.get('max_calls_per_day')} 次")
        return 0
    ok, why = budget.can_call(ledger, b["cap_cny"], b.get("reserve_cny", 0.0))
    if not ok:
        print("SKIP: " + why)
        return 0
    user = json.dumps(payload, ensure_ascii=False)

    result_doc = {"at": C.iso(now), "verdict": verdict, "signature": signature,
                  "model": b.get("model"), "status": "skipped", "triage": None,
                  "budget": budget.summary(ledger, b["cap_cny"])}
    try:
        resp = _post_deepseek(cfg, [{"role": "system", "content": SYSTEM_PROMPT},
                                    {"role": "user", "content": user}])
        usage = resp.get("usage") or {}
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        triage = _parse_json_text(text)
        entry = budget.record(args.root, b.get("model", "deepseek-chat"), usage,
                              f"triage:{verdict}:{signature}", prices=b.get("prices"),
                              cap_cny=b["cap_cny"], now=now)
        result_doc.update({"status": "ok", "triage": triage, "usage": usage,
                           "cost_cny": entry["cost_cny"]})
        print(f"DeepSeek 诊断完成：{usage.get('prompt_tokens')}+{usage.get('completion_tokens')} tokens，"
              f"本次约 {entry['cost_cny']:.4f} 元")
        print(json.dumps(triage, ensure_ascii=False, indent=2)[:3000])
    except Exception as e:  # noqa: BLE001 - record and keep the workflow informational
        result_doc.update({"status": "error", "error": f"{type(e).__name__}: {e}"})
        print("DeepSeek 调用失败：" + str(e))

    ledger = budget.load_ledger(args.root, now)
    result_doc["budget"] = budget.summary(ledger, b["cap_cny"])
    C.dump_json(os.path.join(args.root, "state", "triage", "latest.json"), result_doc)
    log = os.path.join(args.root, "logs", "triage.jsonl")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({k: v for k, v in result_doc.items() if k != "triage"}, ensure_ascii=False) + "\n")

    for line in budget.commit_with_merge(args.root, cfg, now=now):
        print(line)
    for line in ghcommit.commit_files(args.root, cfg, ["state/triage/latest.json"],
                                      f"ops: triage {verdict} ({signature[:40]})"):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
