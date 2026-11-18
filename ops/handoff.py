"""GPT ↔ DeepSeek 任务交接：用文件做协议，不依赖聊天上下文。

三个目录（都在本仓库内，公开可见，禁止写入任何密钥/会员数据）：

    tasks/    待办工单，人或 GPT 创建，状态机 queued -> claimed -> done/failed/blocked
    reports/  执行方（DeepSeek/巡检）产出的结论报告，JSON + Markdown
    state/    运行时状态：最近巡检、预算账本、诊断结果

第一阶段的 scope 只允许 "read-only"：任何写生产、改算法、改历史数据的工单会被拒绝。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import common as C  # noqa: E402

PRIORITIES = ("P0", "P1", "P2")
STATUSES = ("queued", "claimed", "done", "failed", "blocked")
ALLOWED_SCOPES = ("read-only",)
TASK_RE = re.compile(r"^TASK-(\d{8})-(\d{3})$")
REPORT_RE = re.compile(r"^RPT-(\d{8})-(\d{3})$")


def dirs(root: str) -> dict:
    return {"tasks": os.path.join(root, "tasks"),
            "reports": os.path.join(root, "reports"),
            "state": os.path.join(root, "state")}


def _ensure(root: str) -> dict:
    d = dirs(root)
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    return d


def _next_id(directory: str, prefix: str, regex, day: str) -> str:
    seq = 0
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            m = regex.match(name.replace(".json", ""))
            if m and m.group(1) == day:
                seq = max(seq, int(m.group(2)))
    return f"{prefix}-{day}-{seq + 1:03d}"


def validate_task(doc: dict) -> list[str]:
    errs = []
    tid = str(doc.get("id", ""))
    if not TASK_RE.match(tid):
        errs.append("id 必须形如 TASK-YYYYMMDD-NNN")
    for key in ("title", "created_by", "created_at", "priority", "scope", "status"):
        if not doc.get(key):
            errs.append(f"缺少字段 {key}")
    if doc.get("priority") not in PRIORITIES:
        errs.append("priority 必须是 P0/P1/P2")
    if doc.get("status") not in STATUSES:
        errs.append("status 非法")
    if doc.get("scope") not in ALLOWED_SCOPES:
        errs.append("第一阶段 scope 只允许 read-only")
    if not str(doc.get("acceptance", "")).strip():
        errs.append("缺少验收标准 acceptance")
    return errs


def new_task(root: str, title: str, created_by: str = "gpt", priority: str = "P1",
             scope: str = "read-only", evidence=None, acceptance: str = "",
             notes: str = "", now=None) -> dict:
    now = now or C.now_utc()
    d = _ensure(root)
    day = now.astimezone(C.BJ).strftime("%Y%m%d")
    doc = {
        "id": _next_id(d["tasks"], "TASK", TASK_RE, day),
        "title": title,
        "created_by": created_by,
        "created_at": now.isoformat(timespec="seconds"),
        "priority": priority,
        "scope": scope,
        "status": "queued",
        "assignee": "",
        "sla_minutes": 15 if priority == "P0" else 60 if priority == "P1" else 240,
        "evidence": evidence or [],
        "acceptance": acceptance or "给出结论、证据链接与下一步动作",
        "notes": notes,
        "links": [],
    }
    errs = validate_task(doc)
    if errs:
        raise ValueError("; ".join(errs))
    C.dump_json(os.path.join(d["tasks"], doc["id"] + ".json"), doc)
    return doc


def load_task(root: str, task_id: str) -> dict:
    path = os.path.join(dirs(root)["tasks"], task_id + ".json")
    doc = C.load_json(path)
    if not isinstance(doc, dict):
        raise FileNotFoundError(task_id)
    return doc


def save_task(root: str, doc: dict) -> None:
    C.dump_json(os.path.join(dirs(root)["tasks"], doc["id"] + ".json"), doc)


def list_tasks(root: str, status: str | None = None) -> list[dict]:
    d = dirs(root)["tasks"]
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        doc = C.load_json(os.path.join(d, name))
        if isinstance(doc, dict) and (status is None or doc.get("status") == status):
            out.append(doc)
    return out


def claim(root: str, task_id: str, by: str = "deepseek", now=None) -> dict:
    now = now or C.now_utc()
    doc = load_task(root, task_id)
    if doc.get("status") != "queued":
        raise ValueError(f"{task_id} 当前状态 {doc.get('status')}，不可领取")
    doc.update({"status": "claimed", "assignee": by,
                "claimed_at": now.isoformat(timespec="seconds")})
    save_task(root, doc)
    return doc


def complete(root: str, task_id: str, verdict: str, summary: str, findings=None,
             evidence=None, next_actions=None, author: str = "deepseek", now=None) -> dict:
    now = now or C.now_utc()
    d = _ensure(root)
    doc = load_task(root, task_id)
    day = now.astimezone(C.BJ).strftime("%Y%m%d")
    report = {
        "report_id": _next_id(d["reports"], "RPT", REPORT_RE, day),
        "task_id": task_id,
        "author": author,
        "created_at": now.isoformat(timespec="seconds"),
        "verdict": verdict,
        "summary": summary,
        "findings": findings or [],
        "evidence": evidence or [],
        "next_actions": next_actions or [],
    }
    C.dump_json(os.path.join(d["reports"], report["report_id"] + ".json"), report)
    with open(os.path.join(d["reports"], report["report_id"] + ".md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(f"# {report['report_id']} · {task_id}\n\n")
        f.write(f"- 结论：**{verdict}**\n- 作者：{author}\n- 时间：{report['created_at']}\n\n")
        f.write(f"## 摘要\n\n{summary}\n\n")
        if findings:
            f.write("## 发现\n\n")
            for x in findings:
                f.write(f"- {x}\n")
            f.write("\n")
        if evidence:
            f.write("## 证据\n\n")
            for x in evidence:
                f.write(f"- {x}\n")
            f.write("\n")
        if next_actions:
            f.write("## 下一步\n\n")
            for x in next_actions:
                f.write(f"- {x}\n")
    doc.update({"status": "done" if verdict.upper() in ("OK", "PASS", "DONE") else "failed",
                "completed_at": report["created_at"], "report_id": report["report_id"]})
    save_task(root, doc)
    return report


def board(root: str) -> str:
    rows = list_tasks(root)
    lines = ["| 任务 | 优先级 | 状态 | 负责人 | 标题 |", "| --- | --- | --- | --- | --- |"]
    for t in rows:
        lines.append(f"| {t['id']} | {t['priority']} | {t['status']} | {t.get('assignee') or '-'} | {t['title']} |")
    if len(lines) == 2:
        lines.append("| - | - | - | - | 暂无任务 |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GPT ↔ DeepSeek 任务交接")
    ap.add_argument("--root", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="创建工单")
    p.add_argument("--title", required=True)
    p.add_argument("--by", default="gpt")
    p.add_argument("--priority", default="P1", choices=PRIORITIES)
    p.add_argument("--scope", default="read-only", choices=ALLOWED_SCOPES)
    p.add_argument("--evidence", action="append", default=[])
    p.add_argument("--acceptance", default="")
    p.add_argument("--notes", default="")

    p = sub.add_parser("list", help="列出工单")
    p.add_argument("--status", default=None)

    p = sub.add_parser("claim", help="领取工单")
    p.add_argument("task_id")
    p.add_argument("--by", default="deepseek")

    p = sub.add_parser("done", help="完成工单并产出报告")
    p.add_argument("task_id")
    p.add_argument("--verdict", default="OK")
    p.add_argument("--summary", required=True)
    p.add_argument("--finding", action="append", default=[])
    p.add_argument("--evidence", action="append", default=[])
    p.add_argument("--next", action="append", default=[])
    p.add_argument("--by", default="deepseek")

    sub.add_parser("board", help="输出看板")
    p = sub.add_parser("validate", help="校验全部工单")
    p.set_defaults()

    args = ap.parse_args(argv)
    root = args.root

    if args.cmd == "new":
        doc = new_task(root, args.title, created_by=args.by, priority=args.priority,
                       scope=args.scope, evidence=args.evidence,
                       acceptance=args.acceptance, notes=args.notes)
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        for t in list_tasks(root, args.status):
            print(f"{t['id']} [{t['priority']}] {t['status']:<8} {t.get('assignee') or '-':<10} {t['title']}")
    elif args.cmd == "claim":
        print(json.dumps(claim(root, args.task_id, by=args.by), ensure_ascii=False, indent=2))
    elif args.cmd == "done":
        rep = complete(root, args.task_id, args.verdict, args.summary,
                       findings=args.finding, evidence=args.evidence,
                       next_actions=args.next, author=args.by)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.cmd == "board":
        print(board(root))
    elif args.cmd == "validate":
        bad = 0
        for t in list_tasks(root):
            errs = validate_task(t)
            if errs:
                bad += 1
                print(f"INVALID {t.get('id')}: {'; '.join(errs)}")
        print(f"checked={len(list_tasks(root))} invalid={bad}")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
