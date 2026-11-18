"""Cost-gated DeepSeek triage: only runs when the inspector reports an anomaly.

Guarantees
----------
* Never calls DeepSeek when the verdict is OK.
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
    "你是「九十刻度 4.0」的只读运维诊断助手。你只能根据给定的巡检 JSON 推理，"
    "严禁编造不存在的监控项、数据或命令输出。"
    "输出必须是单个 JSON 对象，字段："
    '{"summary":"一句话结论",'
    '"root_causes":[{"cause":"最可能根因","confidence":0到1,"evidence":"对应巡检字段/值"}],'
    '"diagnose_steps":[{"command":"只读排查命令","purpose":"要确认什么"}],'
    '"suggested_fix":{"action":"建议的人工修复动作","risk":"low|medium|high","needs_approval":true},'
    '"customer_impact":"对会员/网站的影响",'
    '"escalate":true或false}。'
    "要求：根因按可能性排序，最多 3 条；命令只允许只读（curl/git log/python 读取），"
    "不得包含生产写入、删除、重启、改算法或改历史数据的操作；"
    "如果证据不足，把 confidence 调低并在 summary 中说明还缺哪些证据。"
)


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
    return sum(1 for e in ledger.get("entries", []) if str(e.get("at", ""))[:10] == day)


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
    ap.add_argument("--force", action="store_true", help="ignore dedupe only (manual test only; budget still enforced)")
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

    if verdict in ("OK", "UNKNOWN") and not args.force:
        print(f"verdict={verdict}：无异常，不调用 DeepSeek（本月已用 {budget.summary(budget.load_ledger(args.root, now), b['cap_cny'])['spent_cny']:.4f} 元）")
        return 0

    ledger = budget.load_ledger(args.root, now)
    if not args.force:
        window = b["fail_dedupe_min"] if verdict == "FAIL" else b["warn_dedupe_min"]
        hit = _dedupe_hit(ledger, signature, float(window), now)
        if hit:
            print("SKIP: " + hit)
            return 0
    if True:
        if _today_entries(ledger, now) >= int(b.get("max_calls_per_day", 12)):
            print(f"SKIP: 今日 AI 调用已达上限 {b.get('max_calls_per_day')} 次")
            return 0
        ok, why = budget.can_call(ledger, b["cap_cny"], b.get("reserve_cny", 0.0))
        if not ok:
            print("SKIP: " + why)
            return 0

    payload = {
        "verdict": verdict,
        "beijing_time": doc.get("beijing_time"),
        "in_sale_window": doc.get("in_sale_window"),
        "failed": doc.get("failed"),
        "warned": doc.get("warned"),
        "checks": [
            {k: v for k, v in c.items() if k not in ("probes",)}
            for c in doc.get("checks", [])
            if c.get("status") in ("fail", "warn")
        ],
    }
    for c in payload["checks"]:
        c["detail"] = str(c.get("detail", ""))[:1200]
    user = ("以下是「九十刻度 4.0」最近一次只读巡检中判定为 fail/warn 的检查项。"
            "请按系统要求输出 JSON 诊断：\n" + json.dumps(payload, ensure_ascii=False)[:6000])

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

    files = [f"state/budget/{budget.month_key(now)}.json", "state/triage/latest.json"]
    for line in ghcommit.commit_files(args.root, cfg, files, f"ops: triage {verdict} ({signature[:40]})"):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
